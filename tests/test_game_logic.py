import pytest
import redis
from concurrent.futures import ThreadPoolExecutor

import app as app_module
from app import app, r, generate_game_hash, all_letters


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


@pytest.fixture(autouse=True)
def clean_redis():
    r.flushdb()


def test_hash_changes_when_letters_are_removed():
    game_code = "HASH42"
    word = "casa"
    r.set(f"game:{game_code}", "admin", ex=3600)
    r.set(f"game:{game_code}:status", "playing", ex=3600)
    r.set(f"game:{game_code}:word", word, ex=3600)
    r.set(f"game:{game_code}:playerplay", 0, ex=3600)
    r.lpush(f"game:{game_code}:players", "A", "B")
    r.expire(f"game:{game_code}:players", 3600)
    r.rpush(f"game:{game_code}:{word}", *all_letters)
    r.expire(f"game:{game_code}:{word}", 3600)

    old_hash = generate_game_hash(game_code)
    r.lrem(f"game:{game_code}:{word}", 0, "a")

    assert old_hash != generate_game_hash(game_code)


def test_all_players_skip_does_not_award_points_and_keeps_current_player(client):
    game_code = "SKIP01"
    word = "casa"
    r.set(f"game:{game_code}", "A", ex=3600)
    r.set(f"game:{game_code}:status", "playing", ex=3600)
    r.set(f"game:{game_code}:word", word, ex=3600)
    r.set(f"game:{game_code}:playerplay", 0, ex=3600)
    r.set(f"game:{game_code}:money", 100, ex=3600)
    r.set(f"game:{game_code}:nb_words", 2, ex=3600)
    r.lpush(f"game:{game_code}:players", "B", "A")
    r.expire(f"game:{game_code}:players", 3600)
    r.rpush(f"game:{game_code}:{word}", *all_letters)
    r.expire(f"game:{game_code}:{word}", 3600)

    client.set_cookie("username", "A", domain="localhost")
    resp = client.post("/guess", data={"game_code": game_code, "action": "skip"})
    assert resp.status_code == 302

    client.set_cookie("username", "B", domain="localhost")
    resp = client.post("/guess", data={"game_code": game_code, "action": "skip"})
    assert resp.status_code == 302

    assert r.get(f"game:{game_code}:score:A") is None
    assert r.get(f"game:{game_code}:score:B") is None
    assert r.get(f"game:{game_code}:playerplay") == "0"


def test_resolve_redis_host_strips_whitespace(monkeypatch):
    monkeypatch.setenv("REDIS_HOST", " redis ")
    assert app_module.resolve_redis_host() == "redis"


def test_delete_game_room_releases_player_usernames():
    game_code = "ROOM99"
    username = "alice"
    r.set(f"game:{game_code}", username, ex=3600)
    r.set(f"game:{game_code}:status", "waiting", ex=3600)
    r.lpush(f"game:{game_code}:players", username)
    r.set(f"user:{username}", username, ex=3600)

    app_module.delete_game_room(game_code)

    assert r.get(f"user:{username}") is None
    assert not r.exists(f"game:{game_code}:players")


def test_delete_game_room_does_not_delete_room_with_longer_code():
    r.set("game:1234", "admin", ex=3600)
    r.set("game:12345", "other-admin", ex=3600)
    r.rpush("game:12345:players", "other-admin")

    app_module.delete_game_room("1234")

    assert r.get("game:1234") is None
    assert r.get("game:12345") == "other-admin"
    assert r.lrange("game:12345:players", 0, -1) == ["other-admin"]


def test_secret_key_uses_shared_environment_value(monkeypatch):
    monkeypatch.setenv("APP_MODE", "prod")
    monkeypatch.setenv("SECRET_KEY", "shared-production-secret")

    assert app_module.resolve_secret_key() == "shared-production-secret"


def test_production_generates_shared_secret_key_in_redis(monkeypatch):
    monkeypatch.setenv("APP_MODE", "prod")
    monkeypatch.delenv("SECRET_KEY", raising=False)
    app_module.r.delete("app:secret_key")

    first = app_module.resolve_secret_key()
    second = app_module.resolve_secret_key()

    # La clé générée est stable et partagée entre les appels (donc entre les workers)
    assert first == second
    assert len(first) >= 32
    app_module.r.delete("app:secret_key")


def test_valid_username_session_is_persistent(client):
    r.set("user:alice", "alice", ex=3600)
    with client.session_transaction() as current_session:
        current_session["username"] = "alice"
    app.config["TESTING"] = False
    try:
        resp = client.get("/")
        with client.session_transaction() as current_session:
            assert current_session.permanent
    finally:
        app.config["TESTING"] = True

    assert resp.status_code == 200


def test_skip_is_rejected_after_game_is_finished(client):
    game_code = "SKIPEND"
    r.set(f"game:{game_code}:status", "finished", ex=3600)
    r.set(f"game:{game_code}:word", "casa", ex=3600)
    r.set(f"game:{game_code}:playerplay", 0, ex=3600)
    r.rpush(f"game:{game_code}:players", "alice")
    r.sadd(f"game:{game_code}:skip_votes", "alice")
    client.set_cookie("username", "alice", domain="localhost")

    resp = client.post("/guess", data={"game_code": game_code, "action": "skip"})

    assert resp.status_code == 302
    assert r.get(f"game:{game_code}:word") == "casa"
    assert r.smembers(f"game:{game_code}:skip_votes") == {"alice"}


def test_guess_normalizes_player_index_after_player_leaves(client):
    game_code = "STALETURN"
    word = "casa"
    r.set(f"game:{game_code}:status", "playing", ex=3600)
    r.set(f"game:{game_code}:word", word, ex=3600)
    r.set(f"game:{game_code}:playerplay", 1, ex=3600)
    r.set(f"game:{game_code}:money", 100, ex=3600)
    r.rpush(f"game:{game_code}:players", "alice")
    r.rpush(f"game:{game_code}:{word}", *all_letters)
    client.set_cookie("username", "alice", domain="localhost")

    resp = client.post("/guess", data={"game_code": game_code, "letter": "c"})

    assert resp.status_code == 302
    assert r.get(f"game:{game_code}:playerplay") == "0"
    assert r.get(f"game:{game_code}:score:alice") == "100"


def test_stale_session_username_is_cleared_without_reservation(client):
    with client.session_transaction() as current_session:
        current_session["username"] = "alice"
    app.config["TESTING"] = False
    try:
        resp = client.post("/newgame")
        with client.session_transaction() as current_session:
            assert "username" not in current_session
    finally:
        app.config["TESTING"] = True

    assert resp.status_code == 302
    assert not r.exists("user:alice")
    assert not list(r.scan_iter(match="game:*"))


def test_finished_scoreboard_requires_room_membership(client):
    game_code = "FINISH1"
    r.set(f"game:{game_code}", "alice", ex=3600)
    r.set(f"game:{game_code}:status", "finished", ex=3600)
    r.set(f"game:{game_code}:word", "secret", ex=3600)
    r.rpush(f"game:{game_code}:players", "alice")
    client.set_cookie("username", "intruder", domain="localhost")
    client.set_cookie("game", game_code, domain="localhost")

    resp = client.get("/game")

    assert resp.status_code == 302
    assert b"secret" not in resp.data


def test_restart_is_rejected_before_game_is_finished(client):
    game_code = "RESTART1"
    r.set(f"game:{game_code}", "alice", ex=3600)
    r.set(f"game:{game_code}:status", "playing", ex=3600)
    r.set(f"game:{game_code}:word", "secret", ex=3600)
    r.rpush(f"game:{game_code}:players", "alice")
    client.set_cookie("username", "alice", domain="localhost")

    resp = client.post("/restartgame", data={"game_code": game_code})

    assert resp.status_code == 302
    assert r.get(f"game:{game_code}:status") == "playing"
    assert r.get(f"game:{game_code}:word") == "secret"


def test_letter_action_does_not_mutate_during_round_transition(client):
    game_code = "LETTERLOCK"
    word = "casa"
    r.set(f"game:{game_code}:status", "playing", ex=3600)
    r.set(f"game:{game_code}:word", word, ex=3600)
    r.set(f"game:{game_code}:playerplay", 0, ex=3600)
    r.set(f"game:{game_code}:money", 100, ex=3600)
    r.rpush(f"game:{game_code}:players", "alice")
    r.rpush(f"game:{game_code}:{word}", *all_letters)
    r.set(f"game:{game_code}:skip_transition", "transition", ex=30)
    client.set_cookie("username", "alice", domain="localhost")

    resp = client.post("/guess", data={"game_code": game_code, "letter": "c"})

    assert resp.status_code == 302
    assert "c" in r.lrange(f"game:{game_code}:{word}", 0, -1)
    assert r.get(f"game:{game_code}:score:alice") is None


def test_finished_admin_leave_form_deletes_room(client):
    game_code = "LEAVEFIN"
    r.set(f"game:{game_code}", "alice", ex=3600)
    r.set(f"game:{game_code}:status", "finished", ex=3600)
    r.rpush(f"game:{game_code}:players", "alice")
    client.set_cookie("username", "alice", domain="localhost")
    client.set_cookie("game", game_code, domain="localhost")

    resp = client.get("/game")

    assert resp.status_code == 200
    assert b'name="action" value="delete"' in resp.data


def test_concurrent_final_skip_votes_transition_word_once(client, monkeypatch):
    game_code = "SKIPRACE"
    r.set(f"game:{game_code}:status", "playing", ex=3600)
    r.set(f"game:{game_code}:word", "casa", ex=3600)
    r.set(f"game:{game_code}:playerplay", 0, ex=3600)
    r.set(f"game:{game_code}:nb_words", 2, ex=3600)
    r.rpush(f"game:{game_code}:players", "alice", "bob")
    transitions = []

    def load_word_once(code, previous_word=None):
        transitions.append(code)
        r.set(f"game:{code}:word", "moto", ex=3600)
        return "moto"

    monkeypatch.setattr(app_module, "load_new_round_word", load_word_once)

    def cast_vote(username):
        with app.test_client() as player_client:
            player_client.set_cookie("username", username, domain="localhost")
            return player_client.post("/guess", data={"game_code": game_code, "action": "skip"})

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(cast_vote, ["alice", "bob"]))

    assert all(response.status_code == 302 for response in responses)
    assert transitions == [game_code]
    assert r.get(f"game:{game_code}:word") == "moto"


def test_cursor_endpoints_require_room_membership(client):
    game_code = "CURSOR1"
    r.rpush(f"game:{game_code}:players", "alice")
    client.set_cookie("username", "intruder", domain="localhost")

    update_resp = client.post("/api/cursor", data={"game_code": game_code, "x": 20, "y": 30})
    list_resp = client.post("/api/cursors", data={"game_code": game_code})

    assert update_resp.status_code == 403
    assert list_resp.status_code == 403
    assert r.get(f"game:{game_code}:cursor:intruder") is None


def test_cursor_listing_uses_room_index_instead_of_scanning_all_redis_keys(client, monkeypatch):
    game_code = "CURSOR2"
    r.rpush(f"game:{game_code}:players", "alice", "bob")
    r.set(f"game:{game_code}:playerplay", 0, ex=3600)
    r.set(f"game:{game_code}:cursor:bob", '{"x":25,"y":40}', ex=3600)
    r.sadd(app_module.cursor_users_key(game_code), "bob")
    client.set_cookie("username", "alice", domain="localhost")
    monkeypatch.setattr(app_module.r, "keys", lambda *args, **kwargs: pytest.fail("keys() doit rester inutilisé"))

    resp = client.post("/api/cursors", data={"game_code": game_code})

    assert resp.status_code == 200
    assert resp.json["cursors"][0]["username"] == "bob"


def test_leaving_room_cleans_presence_and_cursor_records(client):
    game_code = "LEAVECURSOR"
    username = "alice"
    r.rpush(f"game:{game_code}:players", username, "bob")
    r.set(app_module.player_presence_key(game_code, username), "1", ex=3600)
    r.set(f"game:{game_code}:cursor:{username}", '{"x":25,"y":40}', ex=3600)
    r.sadd(app_module.cursor_users_key(game_code), username, "bob")
    client.set_cookie("username", username, domain="localhost")

    resp = client.post("/leavegame", data={"game_code": game_code})

    assert resp.status_code == 302
    assert r.get(app_module.player_presence_key(game_code, username)) is None
    assert r.get(f"game:{game_code}:cursor:{username}") is None
    assert username not in r.smembers(app_module.cursor_users_key(game_code))


def test_leaving_room_preserves_current_player_turn(client):
    game_code = "LEAVETURN"
    r.set(f"game:{game_code}:status", "playing", ex=3600)
    r.set(f"game:{game_code}:playerplay", 2, ex=3600)
    r.rpush(f"game:{game_code}:players", "alice", "bob", "carol")
    client.set_cookie("username", "bob", domain="localhost")

    resp = client.post("/leavegame", data={"game_code": game_code})

    assert resp.status_code == 302
    assert r.lrange(f"game:{game_code}:players", 0, -1) == ["alice", "carol"]
    assert r.get(f"game:{game_code}:playerplay") == "1"


def test_leavegame_rejects_non_member_without_releasing_username(client):
    other_game_code = "OTHERROOM"
    r.rpush(f"game:{other_game_code}:players", "alice")
    r.set("user:intruder", "intruder", ex=3600)
    client.set_cookie("username", "intruder", domain="localhost")

    resp = client.post("/leavegame", data={"game_code": other_game_code})

    assert resp.status_code == 302
    assert r.lrange(f"game:{other_game_code}:players", 0, -1) == ["alice"]
    assert r.get("user:intruder") == "intruder"


def test_deleted_room_shows_room_deleted_message(client):
    game_code = "ROOM88"
    username = "alice"
    client.set_cookie("username", username, domain="localhost")
    client.set_cookie("game", game_code, domain="localhost")

    resp = client.get("/waiting", follow_redirects=True)

    assert resp.status_code == 200
    assert b"La salle" in resp.data
    assert game_code.encode() in resp.data
    assert b"supprim\xc3\xa9e." in resp.data


def test_setusername_rejects_username_already_claimed_by_someone_else(client):
    r.set("user:Bryan_Drouet2", "Bryan_Drouet2", ex=3600)

    resp = client.post("/setusername", data={"username": "Bryan_Drouet2"}, follow_redirects=True)

    assert resp.status_code == 200
    assert b"est d\xc3\xa9j\xc3\xa0 pris." in resp.data
    assert "username" not in resp.request.cookies


def test_forged_username_cookie_cannot_renew_someone_elses_reservation(client):
    r.set("user:alice", "alice", ex=3600)
    app.config["TESTING"] = False
    try:
        client.set_cookie("username", "alice", domain="localhost")
        resp = client.post("/setusername", data={"username": "alice"})
    finally:
        app.config["TESTING"] = True

    assert resp.status_code == 302
    assert r.ttl("user:alice") <= 3600
    assert b"d\xc3\xa9j\xc3\xa0 pris" in client.get("/", follow_redirects=True).data


def test_pages_are_not_cached_so_cleared_cookies_show_no_pseudo(client):
    resp = client.get("/")

    assert resp.headers["Cache-Control"] == "no-store, no-cache, must-revalidate"
    assert b'value="player_' in resp.data


def test_home_tabs_expose_aria_relationships(client):
    resp = client.get("/")

    assert b'role="tablist"' in resp.data
    assert b'role="tab" aria-selected="true" aria-controls="join-tab"' in resp.data
    assert b'role="tabpanel" aria-labelledby="join-tab-button"' in resp.data


def test_new_visitors_get_a_unique_reserved_default_username(client):
    resp1 = client.get("/")
    assigned1 = resp1.headers.getlist("Set-Cookie")
    username1 = next(c.split("=", 1)[1].split(";", 1)[0] for c in assigned1 if c.startswith("username="))

    with app.test_client() as other_client:
        resp2 = other_client.get("/")
        assigned2 = resp2.headers.getlist("Set-Cookie")
        username2 = next(c.split("=", 1)[1].split(";", 1)[0] for c in assigned2 if c.startswith("username="))

    assert username1 != username2
    assert r.get(f"user:{username1}") == username1
    assert r.get(f"user:{username2}") == username2


def test_joining_game_silently_renews_username_reservation(client):
    # La réservation existe déjà avec un TTL court : rejoindre une partie doit la prolonger.
    r.set("user:alice", "alice", ex=60)
    client.set_cookie("username", "alice", domain="localhost")

    resp = client.post("/joingame", data={"game_code": "1234"}, follow_redirects=True)

    assert resp.status_code == 200
    assert b"Veuillez valider votre pseudo" not in resp.data
    assert r.get("user:alice") == "alice"
    assert r.ttl("user:alice") > 60


def test_username_renewal_never_overwrites_someone_elses_reservation():
    r.set("user:alice", "someone-else", ex=60)

    assert app_module.renew_username_reservation("alice") is False
    assert r.get("user:alice") == "someone-else"
    assert r.ttl("user:alice") <= 60

    r.set("user:alice", "alice", ex=60)

    assert app_module.renew_username_reservation("alice") is True
    assert r.ttl("user:alice") > 60

    r.delete("user:alice")

    assert app_module.renew_username_reservation("alice") is False
    assert r.get("user:alice") is None


def test_admin_stays_first_in_players_board_after_others_join(client):
    r.set("user:admin", "admin", ex=3600)
    r.set("user:bob", "bob", ex=3600)
    r.set("user:carol", "carol", ex=3600)

    client.set_cookie("username", "admin", domain="localhost")
    client.post("/newgame")
    game_code = next(key.split(":")[1] for key in r.keys("game:*") if key.count(":") == 1 and r.get(key) == "admin")

    client.set_cookie("username", "bob", domain="localhost")
    client.post("/joingame", data={"game_code": game_code})

    client.set_cookie("username", "carol", domain="localhost")
    client.post("/joingame", data={"game_code": game_code})

    players = r.lrange(f"game:{game_code}:players", 0, -1)
    assert players == ["admin", "bob", "carol"]


def test_resolve_redis_host_uses_docker_internal_in_container(monkeypatch):
    monkeypatch.delenv("REDIS_HOST", raising=False)
    monkeypatch.setattr(app_module.os.path, "exists", lambda path: path == "/.dockerenv")
    assert app_module.resolve_redis_host() == "host.docker.internal"


def test_build_redis_client_falls_back_to_host_docker_internal(monkeypatch):
    attempts = []

    class DummyRedis:
        def __init__(self, host, **kwargs):
            self.host = host
            attempts.append(host)

        def ping(self):
            if self.host == "redis":
                raise redis.exceptions.TimeoutError("Timeout connecting to server")
            if self.host == "host.docker.internal":
                return True
            return True

    monkeypatch.setattr(app_module.redis, "Redis", DummyRedis)
    monkeypatch.setattr(app_module, "resolve_redis_host", lambda: "redis")

    client = app_module.build_redis_client()

    assert client.host == "host.docker.internal"
    assert "redis" in attempts
    assert "host.docker.internal" in attempts


def test_build_redis_client_does_not_fall_back_in_production(monkeypatch):
    attempts = []

    class DummyRedis:
        def __init__(self, host, **kwargs):
            self.host = host
            attempts.append(host)

        def ping(self):
            raise redis.exceptions.TimeoutError("Timeout connecting to server")

    monkeypatch.setenv("APP_MODE", "prod")
    monkeypatch.setattr(app_module.redis, "Redis", DummyRedis)
    monkeypatch.setattr(app_module, "resolve_redis_host", lambda: "redis")

    with pytest.raises(RuntimeError, match="redis"):
        app_module.build_redis_client()

    assert attempts == ["redis"]


def test_same_username_is_allowed_for_current_user(client):
    username = "alice"
    r.set(f"user:{username}", username, ex=3600)
    client.set_cookie("username", username, domain="localhost")

    resp = client.post("/setusername", data={"username": username})

    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/")


def test_set_username_handles_redis_timeout_gracefully(client, monkeypatch):
    def raise_timeout(*args, **kwargs):
        raise redis.exceptions.TimeoutError("Timeout connecting to server")

    monkeypatch.setattr(app_module.r, "get", raise_timeout)
    client.set_cookie("username", "old-user", domain="localhost")

    resp = client.post("/setusername", data={"username": "new-user"})

    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/")


def test_changing_username_releases_previous_name(client):
    old_username = "alice"
    new_username = "bob"
    r.set(f"user:{old_username}", old_username, ex=3600)
    client.set_cookie("username", old_username, domain="localhost")

    resp = client.post("/setusername", data={"username": new_username})

    assert resp.status_code == 302
    assert r.get(f"user:{old_username}") is None
    assert r.get(f"user:{new_username}") == new_username


def test_index_hides_stale_resume_and_unknown_game_flash_is_sentence(client):
    client.set_cookie("game", "2380", domain="localhost")
    client.set_cookie("username", "alice", domain="localhost")
    r.set("user:alice", "alice", ex=3600)

    index_resp = client.get("/")
    assert b"Reprendre la partie" not in index_resp.data

    join_resp = client.post("/joingame", data={"game_code": "2380"}, follow_redirects=True)
    assert b"La partie" in join_resp.data
    assert b"2380" in join_resp.data
    assert b"est inconnue." in join_resp.data


def test_player_can_join_a_game_already_in_progress(client):
    game_code = "INPROG1"
    word = "casa"
    r.set(f"game:{game_code}", "alice", ex=3600)
    r.set(f"game:{game_code}:status", "playing", ex=3600)
    r.set(f"game:{game_code}:word", word, ex=3600)
    r.set(f"game:{game_code}:playerplay", 0, ex=3600)
    r.set(f"game:{game_code}:money", 100, ex=3600)
    r.set(f"game:{game_code}:nb_words", 2, ex=3600)
    r.rpush(f"game:{game_code}:players", "alice")
    r.rpush(f"game:{game_code}:{word}", *all_letters)
    r.expire(f"game:{game_code}:{word}", 3600)
    r.set("user:bob", "bob", ex=3600)

    client.set_cookie("username", "bob", domain="localhost")
    resp = client.post("/joingame", data={"game_code": game_code}, follow_redirects=True)

    assert resp.status_code == 200
    assert b"est inconnue." not in resp.data
    assert "bob" in r.lrange(f"game:{game_code}:players", 0, -1)



def test_all_players_skip_reloads_word_in_redis(client, monkeypatch):
    game_code = "SKIP02"
    old_word = "casa"
    new_word = "moto"
    monkeypatch.setattr(app_module, "get_available_words", lambda exclude=None: new_word)

    r.set(f"game:{game_code}", "A", ex=3600)
    r.set(f"game:{game_code}:status", "playing", ex=3600)
    r.set(f"game:{game_code}:word", old_word, ex=3600)
    r.set(f"game:{game_code}:playerplay", 0, ex=3600)
    r.set(f"game:{game_code}:money", 100, ex=3600)
    r.set(f"game:{game_code}:nb_words", 2, ex=3600)
    r.lpush(f"game:{game_code}:players", "B", "A")
    r.expire(f"game:{game_code}:players", 3600)
    r.rpush(f"game:{game_code}:{old_word}", *all_letters)
    r.expire(f"game:{game_code}:{old_word}", 3600)

    client.set_cookie("username", "A", domain="localhost")
    resp = client.post("/guess", data={"game_code": game_code, "action": "skip"})
    assert resp.status_code == 302

    client.set_cookie("username", "B", domain="localhost")
    resp = client.post("/guess", data={"game_code": game_code, "action": "skip"})
    assert resp.status_code == 302

    assert r.get(f"game:{game_code}:word") == new_word
    assert r.get(f"game:{game_code}:nb_words") == "1"
    assert r.get(f"game:{game_code}:skip_votes") is None
    assert not r.exists(f"game:{game_code}:{old_word}")


def test_skipping_final_word_finishes_game_without_loading_another(client, monkeypatch):
    game_code = "SKIPFINAL"
    word = "casa"
    monkeypatch.setattr(app_module, "load_new_round_word", lambda *args, **kwargs: pytest.fail("Ne doit pas charger de mot supplémentaire"))
    r.set(f"game:{game_code}:status", "playing", ex=3600)
    r.set(f"game:{game_code}:word", word, ex=3600)
    r.set(f"game:{game_code}:playerplay", 0, ex=3600)
    r.set(f"game:{game_code}:nb_words", 1, ex=3600)
    r.rpush(f"game:{game_code}:players", "alice")
    client.set_cookie("username", "alice", domain="localhost")

    resp = client.post("/guess", data={"game_code": game_code, "action": "skip"})

    assert resp.status_code == 302
    assert r.get(f"game:{game_code}:status") == "finished"
    assert r.get(f"game:{game_code}:nb_words") == "0"
    assert r.get(f"game:{game_code}:word") == word


def test_winner_of_the_word_plays_first_on_the_next_round(client, monkeypatch):
    game_code = "WIN01"
    word = "casa"
    new_word = "moto"
    monkeypatch.setattr(app_module, "get_available_words", lambda exclude=None: new_word)

    r.set(f"game:{game_code}", "A", ex=3600)
    r.set(f"game:{game_code}:status", "playing", ex=3600)
    r.set(f"game:{game_code}:word", word, ex=3600)
    r.set(f"game:{game_code}:playerplay", 1, ex=3600)
    r.set(f"game:{game_code}:money", 100, ex=3600)
    r.set(f"game:{game_code}:nb_words", 2, ex=3600)
    r.rpush(f"game:{game_code}:players", "A", "B")
    r.expire(f"game:{game_code}:players", 3600)
    r.rpush(f"game:{game_code}:{word}", *all_letters)
    r.expire(f"game:{game_code}:{word}", 3600)

    # C'est le tour de B, qui devine le mot entier.
    client.set_cookie("username", "B", domain="localhost")
    resp = client.post("/guess", data={"game_code": game_code, "text": word})

    assert resp.status_code == 302
    assert r.get(f"game:{game_code}:word") == new_word
    assert r.get(f"game:{game_code}:playerplay") == "1"
    assert r.get(f"game:{game_code}:score:B") == "400"


def test_winner_gets_points_for_the_final_word(client):
    game_code = "FINAL01"
    word = "casa"
    r.set(f"game:{game_code}", "admin", ex=3600)
    r.set(f"game:{game_code}:status", "playing", ex=3600)
    r.set(f"game:{game_code}:word", word, ex=3600)
    r.set(f"game:{game_code}:playerplay", 0, ex=3600)
    r.set(f"game:{game_code}:money", 200, ex=3600)
    r.set(f"game:{game_code}:nb_words", 1, ex=3600)
    r.rpush(f"game:{game_code}:players", "alice")
    # Aucune lettre jouée : les 4 lettres du mot sont encore masquées (4 x 200 = 800)
    r.rpush(f"game:{game_code}:{word}", *all_letters)

    client.set_cookie("username", "alice", domain="localhost")
    resp = client.post("/guess", data={"game_code": game_code, "text": word})

    assert resp.status_code == 302
    assert r.get(f"game:{game_code}:status") == "finished"
    assert r.get(f"game:{game_code}:score:alice") == "800"
    assert r.get(f"game:{game_code}:nb_words") == "0"


def test_any_player_can_vote_skip_regardless_of_turn(client):
    game_code = "SKIP03"
    word = "casa"
    r.set(f"game:{game_code}", "A", ex=3600)
    r.set(f"game:{game_code}:status", "playing", ex=3600)
    r.set(f"game:{game_code}:word", word, ex=3600)
    r.set(f"game:{game_code}:playerplay", 0, ex=3600)
    r.lpush(f"game:{game_code}:players", "B", "A")
    r.expire(f"game:{game_code}:players", 3600)
    r.rpush(f"game:{game_code}:{word}", *all_letters)
    r.expire(f"game:{game_code}:{word}", 3600)

    # C'est le tour de A, mais B peut quand même voter pour passer immédiatement.
    client.set_cookie("username", "B", domain="localhost")
    resp = client.post("/guess", data={"game_code": game_code, "action": "skip"})

    assert resp.status_code == 302
    assert r.smembers(f"game:{game_code}:skip_votes") == {"B"}
    assert r.get(f"game:{game_code}:playerplay") == "0"


def test_generate_game_hash_does_not_crash_after_a_skip_vote(client):
    game_code = "SKIP04"
    word = "casa"
    r.set(f"game:{game_code}", "A", ex=3600)
    r.set(f"game:{game_code}:status", "playing", ex=3600)
    r.set(f"game:{game_code}:word", word, ex=3600)
    r.set(f"game:{game_code}:playerplay", 0, ex=3600)
    r.lpush(f"game:{game_code}:players", "B", "A")
    r.expire(f"game:{game_code}:players", 3600)
    r.rpush(f"game:{game_code}:{word}", *all_letters)
    r.expire(f"game:{game_code}:{word}", 3600)

    client.set_cookie("username", "A", domain="localhost")
    client.post("/guess", data={"game_code": game_code, "action": "skip"})

    # Générer un hash lisait 'skip_votes' comme une liste alors que c'est un set, ce qui plantait la synchronisation live.
    game_hash = app_module.generate_game_hash(game_code)
    assert isinstance(game_hash, str) and len(game_hash) == 8


def test_finished_page_shows_winner_and_restart_button_for_admin(client):
    game_code = "END01"
    r.set(f"game:{game_code}", "admin", ex=3600)
    r.set(f"game:{game_code}:status", "finished", ex=3600)
    r.set(f"game:{game_code}:word", "casa", ex=3600)
    r.rpush(f"game:{game_code}:players", "admin", "bob")
    r.set(f"game:{game_code}:score:admin", 500, ex=3600)
    r.set(f"game:{game_code}:score:bob", 900, ex=3600)

    client.set_cookie("username", "admin", domain="localhost")
    client.set_cookie("game", game_code, domain="localhost")

    resp = client.get("/game")

    assert resp.status_code == 200
    assert b"Relancer la partie" in resp.data
    assert b'<div class="player-name">bob</div>' in resp.data
    assert resp.data.index(b'<div class="player-name">bob</div>') < resp.data.index(b'<div class="player-name">admin</div>')


def test_restart_game_resets_state_and_returns_to_waiting(client):
    game_code = "END02"
    old_word = "casa"
    r.set(f"game:{game_code}", "admin", ex=3600)
    r.set(f"game:{game_code}:status", "finished", ex=3600)
    r.set(f"game:{game_code}:word", old_word, ex=3600)
    r.rpush(f"game:{game_code}:players", "admin", "bob")
    r.set(app_module.player_presence_key(game_code, "admin"), "1", ex=15)
    r.set(app_module.player_presence_key(game_code, "bob"), "1", ex=15)
    r.set(f"game:{game_code}:rounds_config", 5, ex=3600)
    r.rpush(f"game:{game_code}:{old_word}", *all_letters)
    r.set(f"game:{game_code}:score:admin", 500, ex=3600)
    r.set(f"game:{game_code}:score:bob", 900, ex=3600)
    r.set(f"game:{game_code}:playerplay", 1, ex=3600)

    client.set_cookie("username", "admin", domain="localhost")
    resp = client.post("/restartgame", data={"game_code": game_code}, follow_redirects=True)

    assert resp.status_code == 200
    assert r.get(f"game:{game_code}:status") == "waiting"
    assert r.get(f"game:{game_code}:score:admin") is None
    assert r.get(f"game:{game_code}:score:bob") is None
    assert r.get(f"game:{game_code}:playerplay") is None
    assert r.get(f"game:{game_code}:nb_words") == "5"
    assert r.lrange(f"game:{game_code}:players", 0, -1) == ["admin", "bob"]


def test_restart_game_removes_inactive_players_and_restores_configured_rounds(client, monkeypatch):
    game_code = "END04"
    r.set(f"game:{game_code}", "admin", ex=3600)
    r.set(f"game:{game_code}:status", "finished", ex=3600)
    r.set(f"game:{game_code}:word", "casa", ex=3600)
    r.set(f"game:{game_code}:nb_words", 0, ex=3600)
    r.set(f"game:{game_code}:rounds_config", 3, ex=3600)
    r.rpush(f"game:{game_code}:players", "admin", "bob", "carol")
    r.set(app_module.player_presence_key(game_code, "bob"), "1", ex=15)
    r.set("user:bob", "bob", ex=3600)
    r.set("user:carol", "carol", ex=3600)
    monkeypatch.setattr(app_module, "get_available_words", lambda exclude=None: "moto")

    client.set_cookie("username", "admin", domain="localhost")
    resp = client.post("/restartgame", data={"game_code": game_code})

    assert resp.status_code == 302
    assert r.lrange(f"game:{game_code}:players", 0, -1) == ["admin", "bob"]
    assert r.get(f"game:{game_code}:nb_words") == "3"
    assert r.get(f"game:{game_code}:word") == "moto"
    # Le joueur retiré (carol) récupère son pseudo ; le joueur conservé (bob) le garde.
    assert r.get("user:carol") is None
    assert r.get("user:bob") == "bob"


def test_home_resumes_existing_waiting_game_after_refresh(client):
    game_code = "RESUME1"
    r.set(f"game:{game_code}", "alice", ex=3600)
    r.set(f"game:{game_code}:status", "waiting", ex=3600)
    r.rpush(f"game:{game_code}:players", "alice")
    client.set_cookie("username", "alice", domain="localhost")
    client.set_cookie("game", game_code, domain="localhost")

    resp = client.get("/")

    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/waiting")


def test_room_cookie_alone_cannot_rejoin_a_waiting_game(client):
    game_code = "COOKIE1"
    r.set(f"game:{game_code}", "admin", ex=3600)
    r.set(f"game:{game_code}:status", "waiting", ex=3600)
    r.rpush(f"game:{game_code}:players", "admin")
    client.set_cookie("game", game_code, domain="localhost")
    client.set_cookie("username", "intruder", domain="localhost")

    resp = client.get("/waiting")

    assert resp.status_code == 302
    assert r.lrange(f"game:{game_code}:players", 0, -1) == ["admin"]


def test_finished_page_keeps_game_cookie_for_refresh(client):
    game_code = "RESUME2"
    r.set(f"game:{game_code}", "alice", ex=3600)
    r.set(f"game:{game_code}:status", "finished", ex=3600)
    r.set(f"game:{game_code}:word", "casa", ex=3600)
    r.rpush(f"game:{game_code}:players", "alice")
    client.set_cookie("username", "alice", domain="localhost")
    client.set_cookie("game", game_code, domain="localhost")

    first = client.get("/game")
    second = client.get("/game")

    assert first.status_code == 200
    assert second.status_code == 200
    assert b"Partie RESUME2 termin\xc3\xa9e" in second.data


def test_restart_game_rejects_non_admin(client):
    game_code = "END03"
    r.set(f"game:{game_code}", "admin", ex=3600)
    r.set(f"game:{game_code}:status", "finished", ex=3600)
    r.rpush(f"game:{game_code}:players", "admin", "bob")

    client.set_cookie("username", "bob", domain="localhost")
    resp = client.post("/restartgame", data={"game_code": game_code}, follow_redirects=True)

    assert resp.status_code == 200
    assert r.get(f"game:{game_code}:status") == "finished"

def test_restart_game_rejects_admin_who_left_the_room(client):
    # L'ancien admin a quitté la salle (il n'est plus dans players), mais la clé
    # game:<code> pointe encore vers son pseudo : il ne doit pas pouvoir relancer.
    game_code = "END05"
    r.set(f"game:{game_code}", "admin", ex=3600)
    r.set(f"game:{game_code}:status", "finished", ex=3600)
    r.set(f"game:{game_code}:word", "casa", ex=3600)
    r.rpush(f"game:{game_code}:players", "bob")

    client.set_cookie("username", "admin", domain="localhost")
    resp = client.post("/restartgame", data={"game_code": game_code})

    assert resp.status_code == 302
    assert r.get(f"game:{game_code}:status") == "finished"
    assert r.get(f"game:{game_code}:word") == "casa"
    assert r.lrange(f"game:{game_code}:players", 0, -1) == ["bob"]


def _start_scoring_game(game_code, word, player, money, score=0, revealed=()):
    # Prépare une partie en cours ; "revealed" = lettres déjà jouées (donc plus dans la liste des lettres disponibles).
    r.set(f"game:{game_code}", player, ex=3600)
    r.set(f"game:{game_code}:status", "playing", ex=3600)
    r.set(f"game:{game_code}:word", word, ex=3600)
    r.set(f"game:{game_code}:playerplay", 0, ex=3600)
    r.set(f"game:{game_code}:money", money, ex=3600)
    r.set(f"game:{game_code}:nb_words", 3, ex=3600)
    r.rpush(f"game:{game_code}:players", player)
    r.rpush(f"game:{game_code}:{word}", *[letter for letter in all_letters if letter not in revealed])
    if score:
        r.set(f"game:{game_code}:score:{player}", score, ex=3600)


def test_vowel_pays_wheel_value_for_each_occurrence(client):
    game_code = "VOWEL01"
    _start_scoring_game(game_code, "mettre", "alice", money=300, score=2500)
    client.set_cookie("username", "alice", domain="localhost")

    resp = client.post("/guess", data={"game_code": game_code, "letter": "e"})

    assert resp.status_code == 302
    # 2500 - 2500 (coût de la voyelle) + 2 occurrences x 300
    assert r.get(f"game:{game_code}:score:alice") == "600"


def test_consonant_pays_wheel_value_only_once(client):
    game_code = "CONSO01"
    _start_scoring_game(game_code, "mettre", "alice", money=300)
    client.set_cookie("username", "alice", domain="localhost")

    resp = client.post("/guess", data={"game_code": game_code, "letter": "t"})

    assert resp.status_code == 302
    # "t" apparaît 2 fois dans "mettre" mais ne rapporte qu'une fois la valeur de la roue
    assert r.get(f"game:{game_code}:score:alice") == "300"


def test_round_win_is_wheel_value_times_masked_letters(client):
    game_code = "BONUS01"
    # Consonnes m, t, r déjà jouées : il reste les deux "e" masqués
    _start_scoring_game(game_code, "mettre", "alice", money=500, revealed=("m", "t", "r"))
    client.set_cookie("username", "alice", domain="localhost")

    resp = client.post("/guess", data={"game_code": game_code, "text": "mettre"})

    assert resp.status_code == 302
    assert r.get(f"game:{game_code}:score:alice") == "1000"


def test_round_win_with_every_letter_masked_uses_word_length(client):
    game_code = "BONUS02"
    _start_scoring_game(game_code, "mettre", "alice", money=500)
    client.set_cookie("username", "alice", domain="localhost")

    resp = client.post("/guess", data={"game_code": game_code, "text": "mettre"})

    assert resp.status_code == 302
    assert r.get(f"game:{game_code}:score:alice") == "3000"


def test_restart_resets_every_score_including_stale_ones(client):
    game_code = "RESET01"
    r.set(f"game:{game_code}", "admin", ex=3600)
    r.set(f"game:{game_code}:status", "finished", ex=3600)
    r.set(f"game:{game_code}:word", "casa", ex=3600)
    r.rpush(f"game:{game_code}:players", "admin", "bob")
    r.set(app_module.player_presence_key(game_code, "bob"), "1", ex=15)
    r.set(f"game:{game_code}:score:admin", 800, ex=3600)
    r.set(f"game:{game_code}:score:bob", 400, ex=3600)
    # Score resté en base d'un ancien joueur parti de la salle (absent de la liste des joueurs)
    r.set(f"game:{game_code}:score:ghost", 700, ex=3600)
    client.set_cookie("username", "admin", domain="localhost")

    resp = client.post("/restartgame", data={"game_code": game_code})

    assert resp.status_code == 302
    assert r.keys(f"game:{game_code}:score:*") == []


def test_room_deletion_rejects_admin_who_left_the_room(client):
    game_code = "DELLEFT1"
    r.set(f"game:{game_code}", "admin", ex=3600)
    r.set(f"game:{game_code}:status", "waiting", ex=3600)
    r.rpush(f"game:{game_code}:players", "bob")
    client.set_cookie("username", "admin", domain="localhost")
    client.set_cookie("game", game_code, domain="localhost")

    resp = client.post("/leavegame", data={"game_code": game_code, "action": "delete"})

    assert resp.status_code == 302
    assert r.exists(f"game:{game_code}")
    assert r.get(f"game:{game_code}:status") == "waiting"