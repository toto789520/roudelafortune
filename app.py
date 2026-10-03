from flask import Flask, Response, redirect, render_template, make_response, request, url_for, flash, session
import random
import redis
import os
import pandas
import hashlib
import json
import secrets
import time
from datetime import timedelta


#env
voyelles = ["a", "e", "i", "o", "u", "y"]
consonnes = ["b", "c", "d", "f", "g", "h", "j", "k", "l", "m", "n", "p", "q", "r", "s", "t", "v", "w", "x", "z"]
all_letters = voyelles + consonnes
DEFAULT_ROUNDS = 5
PLAYER_PRESENCE_TTL = 15
SKIP_VOTE_SCRIPT = """
if redis.call('GET', KEYS[1]) ~= 'playing' then
    return {-1, 0}
end
if redis.call('EXISTS', KEYS[4]) == 1 then
    return {-3, 0}
end
local players = redis.call('LRANGE', KEYS[2], 0, -1)
local is_player = false
for _, player in ipairs(players) do
    if player == ARGV[1] then
        is_player = true
        break
    end
end
if not is_player then
    return {-2, 0}
end
redis.call('SADD', KEYS[3], ARGV[1])
local votes = 0
for _, player in ipairs(players) do
    if redis.call('SISMEMBER', KEYS[3], player) == 1 then
        votes = votes + 1
    end
end
if #players > 0 and votes >= #players then
    if redis.call('SET', KEYS[4], ARGV[2], 'NX', 'EX', 30) then
        redis.call('DEL', KEYS[3])
        return {1, votes}
    end
    return {-3, votes}
end
return {0, votes}
"""
print("All letters:", all_letters)
port=int(os.getenv("PORT") or 8000)

# Connexion à Redis
def resolve_redis_host():
    env_host = os.getenv("SERVICE_NAME_REDIS") or os.getenv("REDIS_HOST")
    if env_host:
        return env_host.strip()
    if os.path.exists("/.dockerenv"):
        return "host.docker.internal"
    return "localhost"


# Essaie l'hôte configuré puis une liste de secours, pour tolérer les environnements
# Docker où le nom de service "redis" ne se résout pas toujours (ex. sandbox de dev).
# En production, on ne bascule jamais vers un hôte de secours non configuré : mieux
# vaut échouer au démarrage que de se connecter à une instance Redis inconnue.
def build_redis_client():
    configured_host = resolve_redis_host()
    is_prod = os.getenv("APP_MODE") == "prod"
    host_candidates = []

    if configured_host:
        host_candidates.append(configured_host)

    if not is_prod:
        for fallback in ("redis", "host.docker.internal", "localhost", "127.0.0.1"):
            if fallback not in host_candidates:
                host_candidates.append(fallback)

    for host in dict.fromkeys(host_candidates):
        client = redis.Redis(
            host=host,
            port=int(os.getenv("REDIS_PORT", 6379)),
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        try:
            client.ping()
            print(f"Connected to Redis on {host}")
            return client
        except redis.exceptions.RedisError as exc:
            print(f"Redis unavailable on {host}: {exc}")

    if is_prod:
        raise RuntimeError(f"Impossible de se connecter à Redis sur l'hôte configuré ({configured_host}).")

    return redis.Redis(
        host=configured_host,
        port=int(os.getenv("REDIS_PORT", 6379)),
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )


r = build_redis_client()

# Fonction pour générer un hash unique de l'état de la partie
def generate_game_hash(game_code):
    data = {
        "status": r.get(f"game:{game_code}:status"),
        "word": r.get(f"game:{game_code}:word"),
        "playerplay": r.get(f"game:{game_code}:playerplay"),
        "players": r.lrange(f"game:{game_code}:players", 0, -1),
        "money": r.get(f"game:{game_code}:money"),
        "nb_words": r.get(f"game:{game_code}:nb_words"),
        "letters": r.lrange(f"game:{game_code}:{r.get(f'game:{game_code}:word')}", 0, -1),
        "last_event": r.get(f"game:{game_code}:last_event"),
        "skip_votes": sorted(r.smembers(f"game:{game_code}:skip_votes"))
    }
    data_str = json.dumps(data, sort_keys=True)
    return hashlib.sha256(data_str.encode()).hexdigest()[:8]

app = Flask(__name__)
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(seconds=3600)

random.seed()
secure_random = random.SystemRandom()


# Clé Redis où est conservée la clé secrète générée automatiquement
SECRET_KEY_REDIS_KEY = "app:secret_key"


def resolve_secret_key():
    # 1. Priorité à la variable d'environnement si elle est fournie
    secret_key = os.getenv("SECRET_KEY")
    if secret_key:
        return secret_key
    if os.getenv("APP_MODE") == "prod":
        # 2. En prod sans variable : on génère une clé une seule fois et on la
        # partage via Redis (nx=True : le premier worker écrit, les autres lisent).
        r.set(SECRET_KEY_REDIS_KEY, secrets.token_hex(32), nx=True)
        return r.get(SECRET_KEY_REDIS_KEY)
    # 3. En dev : clé fixe, jamais utilisée en production
    return "development-only-secret-key-change-before-production"


app.secret_key = resolve_secret_key()


def authenticated_username():
    username = session.get("username")
    if app.config.get("TESTING"):
        return username
    if username and r.get(f"user:{username}") == username:
        session.permanent = True
        return username
    session.pop("username", None)
    return None


@app.before_request
def load_test_identity():
    if app.config.get("TESTING"):
        test_username = request.cookies.get("username")
        if test_username:
            session["username"] = test_username


@app.after_request
def add_security_headers(response):
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "form-action 'self'; "
        "base-uri 'self'; "
        "object-src 'none'; "
        "frame-ancestors 'none';"
    )
    # Empêche le navigateur de réafficher une page contenant un ancien pseudo après un clear des cookies.
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


@app.errorhandler(redis.exceptions.RedisError)
def handle_redis_error(error):
    print(f"Redis error: {error}")
    flash("Le serveur Redis est indisponible. Réessayez plus tard.")
    return redirect(url_for("index"))

#pandas connection test
try:
    pd = pandas.read_csv("data/data.csv")
    pd = pd["data"]

    print("Pandas connection successful")

except Exception as e:
    print(f"Error connecting to pandas: {e}")
    pd = None


# On prépare un nouveau mot pour le tour suivant et on remet les lettres à zéro.
def load_new_round_word(game_code, previous_word=None):
    next_word = get_available_words(exclude=previous_word)

    if previous_word:
        r.delete(f"game:{game_code}:{previous_word}")

    r.set(f"game:{game_code}:word", next_word, ex=3600)
    r.delete(f"game:{game_code}:{next_word}")
    r.rpush(f"game:{game_code}:{next_word}", *all_letters)
    r.expire(f"game:{game_code}:{next_word}", 3600)
    return next_word


def get_available_words(exclude=None):
    try:
        words = pandas.read_csv("data/data.csv")["data"].dropna().astype(str).str.strip()
        words = list(dict.fromkeys(word for word in words if word))
        if exclude is not None:
            words = [word for word in words if word != exclude]
        if not words:
            return "defaultword"
        print(f"Successfully read {len(words)} distinct words from CSV.")
        return secure_random.choice(words)
    except Exception as e:
        print(f"Error reading words from CSV: {e}")
        return "defaultword"  # Fallback word list


def count_masked_letters(word, available_letters):
    # Nombre de cases encore masquées dans le mot : une lettre reste masquée tant
    # qu'elle fait partie des lettres pas encore jouées (même logique que l'affichage).
    return sum(1 for char in word if char in available_letters)


def release_username(username):
    if not username:
        return
    r.eval(
        "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) else return 0 end",
        1,
        f"user:{username}",
        username,
    )


# Prolonge la réservation seulement si elle appartient toujours à ce pseudo.
# Le test et l'EXPIRE sont faits en une seule opération Redis, donc un autre visiteur
# qui prend le pseudo entre-temps n'est jamais écrasé.
RENEW_USERNAME_SCRIPT = (
    "if redis.call('GET', KEYS[1]) == ARGV[1] then "
    "return redis.call('EXPIRE', KEYS[1], ARGV[2]) else return 0 end"
)


def renew_username_reservation(username):
    return bool(r.eval(RENEW_USERNAME_SCRIPT, 1, f"user:{username}", username, 3600))


def reserve_default_username(max_attempts=5):
    # Chaque tentative est une réservation atomique pour garantir l'unicité du pseudo par défaut.
    for _ in range(max_attempts):
        candidate = f"player_{secure_random.randint(10000, 99999)}"
        if r.set(f"user:{candidate}", candidate, nx=True, ex=3600):
            return candidate
    return None


def player_presence_key(game_code, username):
    return f"game:{game_code}:presence:{username}"


def cursor_users_key(game_code):
    return f"game:{game_code}:cursor_users"


def delete_game_room(game_code):
    if not game_code:
        return

    players = r.lrange(f"game:{game_code}:players", 0, -1)
    for player in players:
        release_username(player)

    room_keys = [f"game:{game_code}", *r.scan_iter(match=f"game:{game_code}:*")]
    for key in room_keys:
        r.delete(key)


## User logique
@app.route("/")
def index():
    username = authenticated_username()
    game_code = request.cookies.get("game")
    newly_assigned = None

    try:
        if not username:
            newly_assigned = reserve_default_username()
            username = newly_assigned or username

        stale_game = bool(game_code) and not r.exists(f"game:{game_code}:status") and not r.exists(f"game:{game_code}")
        game_status = r.get(f"game:{game_code}:status") if game_code and not stale_game else None
        game_players = r.lrange(f"game:{game_code}:players", 0, -1) if game_status else []
    except redis.exceptions.RedisError:
        return render_template("index.html", code=username, default_username="", game_code=None)

    if game_status in ("waiting", "playing") and username in game_players:
        return redirect(url_for("waiting" if game_status == "waiting" else "game"))
    if game_status == "finished" and username in game_players:
        return redirect(url_for("game"))

    if stale_game:
        flash(f"La salle '{game_code}' a été supprimée.")
        resp = make_response(render_template("index.html", code=username, default_username="", game_code=None))
        resp.set_cookie("game", "", expires=0, secure=request.is_secure, httponly=True)
    else:
        resp = make_response(render_template("index.html", code=username, default_username="", game_code=game_code if game_code else None))

    if newly_assigned:
        session["username"] = newly_assigned
        session.permanent = True
        resp.set_cookie("username", newly_assigned, max_age=3600, secure=request.is_secure, httponly=True)

    return resp

@app.route("/setusername", methods=["GET", "POST"])
@app.route("/setusername/", methods=["GET", "POST"])
def set_username():
    current_username = authenticated_username()

    if request.method == "POST":
        username = request.form.get("username")
    else:
        username = request.args.get("username")
        if not username:
            return redirect(url_for("index"))

    if username is None:
        flash("Le pseudo est obligatoire.")
        return redirect(url_for("index"))

    username = username.strip()

    if not username:
        flash("Le pseudo est obligatoire.")
        return redirect(url_for("index"))

    try:
        if len(username) < 3 or len(username) > 20:
            flash("Le nom d'utilisateur doit contenir entre 3 et 20 caractères.")
            return redirect(url_for("index"))

        if username == current_username and renew_username_reservation(username):
            pass  # réservation prolongée, rien d'autre à faire
        else:
            # Réservation atomique pour éviter que deux clients ne prennent le même pseudo en même temps.
            claimed = r.set(f"user:{username}", username, nx=True, ex=3600)
            if not claimed:
                flash(f"Le pseudo '{username}' est déjà pris.")
                return redirect(url_for("index"))

            # On libère l'ancien pseudo seulement s'il est différent du nouveau
            # (sinon on supprimerait la réservation qu'on vient de reprendre).
            if current_username and current_username != username:
                release_username(current_username)
    except redis.exceptions.RedisError as exc:
        print(f"Redis error while updating username: {exc}")
        flash("Le serveur Redis est indisponible. Réessayez plus tard.")
        return redirect(url_for("index"))

    session["username"] = username
    session.permanent = True
    resp = redirect(url_for("index"))
    resp.set_cookie('username', username, max_age=3600, secure=request.is_secure, httponly=True)
    return resp

## join game logique
@app.route("/newgame", methods=["POST"])
def newgame():
    username = authenticated_username()

    if not username:
        return redirect(url_for("index"))

    # Le pseudo est déjà attribué par défaut, on renouvelle juste sa réservation.
    if not renew_username_reservation(username):
        session.pop("username", None)
        return redirect(url_for("index"))

    if request.cookies.get("game"):
        return redirect(url_for("game"))

    code = str(random.randrange(1111, 9999))

    word = str(get_available_words())  # Récupère un mot aléatoire depuis le CSV

    # Une partie par code, associée à son utilisateur.
    r.set(f"game:{code}", username, ex=3600)
    # On enregistre le mot courant et on crée la liste des lettres encore jouables.
    r.set(f"game:{code}:word", word, ex=3600)
    r.rpush(f"game:{code}:{word}", *all_letters)
    r.expire(f"game:{code}:{word}", 3600)
    # Créer la liste des joueurs pour cette partie
    r.rpush(f"game:{code}:players", username)
    r.expire(f"game:{code}:players", 3600)
    # initialiser le statut de la partie
    r.set(f"game:{code}:status", "waiting", ex=3600)
    r.set(f"game:{code}:money", random.randrange(50, 1000, 50), ex=3600)
    r.set(f"game:{code}:nb_words", DEFAULT_ROUNDS, ex=3600)
    r.set(f"game:{code}:rounds_config", DEFAULT_ROUNDS, ex=3600)

    # Rediriger vers la page de jeu avec le code de la partie dans les cookies

    resp = redirect(url_for("game"))
    resp.set_cookie(
        "game",
        code,
        max_age=3600,
        secure=request.is_secure,
        httponly=True
    )
    return resp

@app.route("/joingame", methods=["POST"])
def joingame(game_code=None):
    username = authenticated_username()

    if not username:
        return redirect(url_for("index"))

    # Le pseudo est déjà attribué par défaut, on renouvelle juste sa réservation.
    if not renew_username_reservation(username):
        session.pop("username", None)
        return redirect(url_for("index"))

    if not game_code:
        game_code = request.form.get("game_code")

    game_status = r.get(f"game:{game_code}:status")
    game_exsit = game_status in ("waiting", "playing") and r.get(f"game:{game_code}") is not None

    if game_exsit:
        resp = redirect(url_for("game"))
        resp.set_cookie(
            "game",
            game_code,
            max_age=3600,
            secure=request.is_secure,
            httponly=True
        )
        if username not in r.lrange(f"game:{game_code}:players", 0, -1):
            r.rpush(f"game:{game_code}:players", username)
        return resp

    flash(f"La partie '{game_code}' est inconnue.")
    return redirect(url_for("index"))

## game logique
@app.route("/waiting", methods=["GET", "POST"])
def waiting():
    game_code = request.cookies.get("game")
    username = authenticated_username()

    if not game_code or not username:
        flash("Partie ou utilisateur introuvable.")
        return redirect(url_for("index"))

    if not r.exists(f"game:{game_code}:status") and not r.exists(f"game:{game_code}"):
        flash(f"La salle '{game_code}' a été supprimée.")
        resp = redirect(url_for("index"))
        resp.set_cookie("game", "", expires=0, secure=request.is_secure, httponly=True)
        return resp

    if r.get(f"game:{game_code}:status") == "playing":
        return redirect(url_for("game"))

    if username not in r.lrange(f"game:{game_code}:players", 0, -1):
        return redirect(url_for("index"))

    if request.form.get("switch_status"):
        if r.get(f"game:{game_code}") == username:
            if r.get(f"game:{game_code}:status") == "waiting":
                r.set(f"game:{game_code}:status", "playing", ex=3600)
                return redirect(url_for("game"))
            else:
                flash(f"Impossible de changer le statut de la partie {game_code}.")
                return redirect(url_for("waiting"))
        else:
            flash(f"Vous n'êtes pas l'administrateur de la partie {game_code}.")
            return redirect(url_for("waiting"))

    if request.form.get("nb_words"):
            if r.get(f"game:{game_code}") == username:
                nb_words = request.form.get("nb_words")
                if nb_words and nb_words.isdigit() and 1 <= int(nb_words) <= 10:
                    r.set(f"game:{game_code}:nb_words", int(nb_words), 3600)
                    r.set(f"game:{game_code}:rounds_config", int(nb_words), ex=3600)
                    return redirect(url_for("game"))
                else:
                    flash(f"Impossible de changer le nombre de mots de la partie {game_code}. Le nombre maximum de manches est de 10.")
                    return redirect(url_for("waiting"))
            else:
                flash(f"Vous n'êtes pas l'administrateur de la partie {game_code}.")
                return redirect(url_for("waiting"))

    if r.get(f"game:{game_code}:status") == "waiting" and username in r.lrange(f"game:{game_code}:players", 0, -1):
        players = r.lrange(f"game:{game_code}:players", 0, -1)
        players_data = [{
            "name": player,
            "score": int(r.get(f"game:{game_code}:score:{player}") or 0),
            "position": idx + 1,
            "is_admin": (r.get(f"game:{game_code}") == player)
        } for idx, player in enumerate(players)]
        return render_template("waiting.html",
                               game_code=game_code,
                               players=players,
                               players_data=players_data,
                               username=username,
                               admin=(r.get(f"game:{game_code}") == username),
                               nb_words=int(r.get(f"game:{game_code}:nb_words") or 1)
                               )

    flash(f"La partie '{game_code}' est inconnue.")
    return redirect(url_for("index"))


@app.route("/leavegame", methods=["POST"])
def leave_game():
    game_code = request.form.get("game_code") or request.cookies.get("game")
    username = authenticated_username()

    if not username or not game_code:
        flash("Partie ou utilisateur introuvable.")
        return redirect(url_for("index"))

    if request.form.get("action") == "delete":
        # Propriétaire de la salle ET toujours présent dans la liste des joueurs
        # (un admin parti via /leavegame garde sinon une clé de propriétaire périmée).
        if username not in r.lrange(f"game:{game_code}:players", 0, -1) or r.get(f"game:{game_code}") != username:
            flash("Vous n'êtes pas l'administrateur de cette salle.")
            return redirect(url_for("waiting"))
        delete_game_room(game_code)
        flash(f"La salle '{game_code}' a été supprimée.")
    else:
        players = r.lrange(f"game:{game_code}:players", 0, -1)
        if username not in players:
            flash("Vous ne faites pas partie de cette salle.")
            return redirect(url_for("index"))

        removed_index = players.index(username)
        game_status = r.get(f"game:{game_code}:status")
        playerplay = r.get(f"game:{game_code}:playerplay")
        current_player = None
        if game_status == "playing" and playerplay and playerplay.isdigit() and int(playerplay) < len(players):
            current_player = players[int(playerplay)]

        r.lrem(f"game:{game_code}:players", 0, username)
        remaining_players = [player for player in players if player != username]
        if game_status == "playing":
            if not remaining_players:
                r.delete(f"game:{game_code}:playerplay")
            elif current_player in remaining_players:
                r.set(f"game:{game_code}:playerplay", remaining_players.index(current_player), ex=3600)
            else:
                r.set(f"game:{game_code}:playerplay", removed_index % len(remaining_players), ex=3600)

        r.srem(f"game:{game_code}:skip_votes", username)
        r.delete(player_presence_key(game_code, username), f"game:{game_code}:cursor:{username}")
        r.srem(cursor_users_key(game_code), username)
        release_username(username)
        flash(f"Vous avez quitté la salle '{game_code}'.")

    resp = redirect(url_for("index"))
    resp.set_cookie("game", "", expires=0, secure=request.is_secure, httponly=True)
    return resp


@app.route("/game")
def game():
    game_code = request.cookies.get("game")
    username = authenticated_username()

    if not game_code or not username:
        flash("Partie ou utilisateur introuvable.")
        return redirect(url_for("index"))

    if not r.exists(f"game:{game_code}:status") and not r.exists(f"game:{game_code}"):
        flash(f"La salle '{game_code}' a été supprimée.")
        resp = redirect(url_for("index"))
        resp.set_cookie("game", "", expires=0, secure=request.is_secure, httponly=True)
        return resp

    if r.get(f"game:{game_code}:status") == "waiting":
        return redirect(url_for("waiting"))

    if r.get(f"game:{game_code}:status") == "playing" and username not in r.lrange(f"game:{game_code}:players", 0, -1):
        return redirect(url_for("index"))

    if r.get(f"game:{game_code}:status") == "playing" and username in r.lrange(f"game:{game_code}:players", 0, -1):
        word = r.get(f"game:{game_code}:word")
        available_letters = r.lrange(f"game:{game_code}:{word}", 0, -1)

        display_word = "".join([
            char if char not in available_letters else (char if char not in all_letters else "X")
            for char in word
        ])

        playerplay = r.get(f"game:{game_code}:playerplay")
        listplayers = r.lrange(f"game:{game_code}:players", 0, -1)

        # Remet le tour à 0 au premier chargement ou si la liste des joueurs a rétréci depuis.
        if not playerplay or int(playerplay) >= len(listplayers):
            r.set(f"game:{game_code}:playerplay", 0, ex=3600)
            playerplay = 0

        ifplay = bool(listplayers) and listplayers[int(playerplay)] == username

        player_index = int(playerplay) if playerplay is not None else 0
        current_player_name = listplayers[player_index] if listplayers else username
        players_data = [{
            "name": player,
            "score": int(r.get(f"game:{game_code}:score:{player}") or 0),
            "position": idx + 1,
            "is_current": idx == player_index,
            "is_me": player == username
        } for idx, player in enumerate(listplayers)]

        last_event_raw = r.get(f"game:{game_code}:last_event")
        last_event = json.loads(last_event_raw) if last_event_raw else None

        skip_votes = set(r.smembers(f"game:{game_code}:skip_votes")) & set(listplayers)

        return render_template("game.html",
                              game_code=game_code,
                              username=username,
                              word=display_word,
                              voyelles=voyelles,
                              consonnes=consonnes,
                              available_letters=available_letters,
                              ifplay=ifplay if 'ifplay' in locals() else False,
                              playerplay=current_player_name,
                              listplayers=listplayers,
                              players_data=players_data,
                              admin=(r.get(f"game:{game_code}") == username),
                              money=int(r.get(f'game:{game_code}:money') or 0),
                              nb_words=int(r.get(f"game:{game_code}:nb_words") or 1),
                              last_event=last_event,
                              skip_votes_count=len(skip_votes),
                              has_voted_skip=username in skip_votes
                              )

    if r.get(f"game:{game_code}:status") == "finished" and username not in r.lrange(f"game:{game_code}:players", 0, -1):
        flash("Vous ne faites pas partie de cette partie.")
        return redirect(url_for("index"))

    if r.get(f"game:{game_code}:status") == "finished":
        scoreboard = sorted(
            [{"name": player, "score": int(r.get(f"game:{game_code}:score:{player}") or 0)} for player in r.lrange(f"game:{game_code}:players", 0, -1)],
            key=lambda entry: entry["score"],
            reverse=True
        )
        for index, entry in enumerate(scoreboard):
            entry["is_winner"] = index == 0

        return render_template("finished.html", game_code=game_code, word=r.get(f"game:{game_code}:word"), listplayers=scoreboard, username=username, admin=(r.get(f"game:{game_code}") == username))

    flash(f"La partie '{game_code}' est inconnue.")
    return redirect(url_for("index"))


@app.route("/restartgame", methods=["POST"])
def restart_game():
    game_code = request.form.get("game_code")
    username = authenticated_username()

    if not game_code or not username:
        flash("Partie ou utilisateur introuvable.")
        return redirect(url_for("index"))

    players = r.lrange(f"game:{game_code}:players", 0, -1)
    if username not in players or r.get(f"game:{game_code}") != username:
        flash("Seul l'administrateur peut relancer la partie.")
        return redirect(url_for("index"))

    if r.get(f"game:{game_code}:status") != "finished":
        flash("La partie n'est pas terminée.")
        return redirect(url_for("game"))

    # Seuls les joueurs dont une page est encore active sont conservés au redémarrage.
    listplayers = r.lrange(f"game:{game_code}:players", 0, -1)
    active_players = [
        player for player in listplayers
        if player == username or r.exists(player_presence_key(game_code, player))
    ]
    departed_players = set(listplayers) - set(active_players)
    r.delete(f"game:{game_code}:players")
    if active_players:
        r.rpush(f"game:{game_code}:players", *active_players)
    for player in departed_players:
        r.delete(f"game:{game_code}:score:{player}", f"game:{game_code}:cursor:{player}")
        r.srem(cursor_users_key(game_code), player)
        # Libère aussi la réservation globale du pseudo, comme /leavegame et delete_game_room.
        release_username(player)

    old_word = r.get(f"game:{game_code}:word")

    # Remise à zéro de TOUS les scores de la salle (y compris ceux d'anciens joueurs
    # partis puis revenus avec le même pseudo), pas seulement ceux des joueurs actifs.
    for score_key in list(r.scan_iter(match=f"game:{game_code}:score:*")):
        r.delete(score_key)

    r.delete(f"game:{game_code}:playerplay")
    r.delete(f"game:{game_code}:last_event")
    r.delete(f"game:{game_code}:skip_votes")
    r.set(f"game:{game_code}:status", "waiting", ex=3600)
    r.set(f"game:{game_code}:money", random.randrange(50, 1000, 50), ex=3600)
    configured_rounds = int(r.get(f"game:{game_code}:rounds_config") or DEFAULT_ROUNDS)
    r.set(f"game:{game_code}:nb_words", configured_rounds, ex=3600)
    load_new_round_word(game_code, old_word)
    r.expire(f"game:{game_code}", 3600)
    r.expire(f"game:{game_code}:players", 3600)

    resp = redirect(url_for("waiting"))
    resp.set_cookie("game", game_code, max_age=3600, secure=request.is_secure, httponly=True)
    return resp




@app.route("/guess", methods=["POST"])
def guess():
    game_code = request.form.get("game_code")
    username = authenticated_username()
    if not game_code or not username:
        flash("Partie ou utilisateur introuvable")
        return redirect(url_for("game"))

    listplayers = r.lrange(f"game:{game_code}:players", 0, -1)
    playerplay = r.get(f"game:{game_code}:playerplay")
    if r.get(f"game:{game_code}:status") != "playing":
        flash("Cette action est disponible uniquement pendant une partie en cours.")
        return redirect(url_for("game"))

    if not listplayers:
        flash("La partie est inconnue ou inactive.")
        return redirect(url_for("game"))

    if username not in listplayers:
        flash("Vous ne faites pas partie de cette partie.")
        return redirect(url_for("game"))

    if request.form.get("action") == "skip":
        lock_key = f"game:{game_code}:skip_transition"
        lock_token = secrets.token_hex(16)
        result, vote_count = r.eval(
            SKIP_VOTE_SCRIPT,
            4,
            f"game:{game_code}:status",
            f"game:{game_code}:players",
            f"game:{game_code}:skip_votes",
            lock_key,
            username,
            lock_token
        )
        if result == -1:
            flash("Cette partie n'est plus en cours.")
        elif result == -2:
            flash("Vous ne faites pas partie de cette partie.")
        elif result == -3:
            flash("Le changement de mot est en cours. Réessayez dans un instant.")
        elif result == 1:
            try:
                remaining_words = int(r.get(f"game:{game_code}:nb_words") or 1) - 1
                r.set(f"game:{game_code}:nb_words", max(remaining_words, 0), ex=3600)
                if remaining_words <= 0:
                    r.set(f"game:{game_code}:status", "finished", ex=3600)
                    flash("Tous les joueurs ont choisi de passer. Aucun point n'a été attribué. La partie est terminée.")
                else:
                    current_word = r.get(f"game:{game_code}:word")
                    load_new_round_word(game_code, current_word)
                    flash(f"Tous les joueurs ont choisi de passer. Aucun point n'a été attribué. Il reste {remaining_words} mots.")
            finally:
                r.eval(
                    "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) else return 0 end",
                    1,
                    lock_key,
                    lock_token
                )
        else:
            flash(f"{username} a voté pour passer ce mot ({vote_count}/{len(listplayers)} votes).")

        return redirect(url_for("game"))

    playerplay = r.get(f"game:{game_code}:playerplay")
    if not playerplay or not playerplay.isdigit() or int(playerplay) >= len(listplayers):
        playerplay = "0"
        r.set(f"game:{game_code}:playerplay", playerplay, ex=3600)

    if listplayers[int(playerplay)] != username:
        flash(f"Ce n'est pas votre tour de jouer, c'est le tour de {listplayers[int(playerplay)]}.")
        return redirect(url_for("game"))

    if request.form.get("letter"):
        letter = request.form.get("letter")

        if not letter:
            flash("Lettre introuvable")
            return redirect(url_for("game"))

        transition_key = f"game:{game_code}:skip_transition"
        transition_token = secrets.token_hex(16)
        if not r.set(transition_key, transition_token, nx=True, ex=30):
            flash("Une autre action de la partie est en cours. Réessayez dans un instant.")
            return redirect(url_for("game"))

        try:
            if r.get(f"game:{game_code}:status") != "playing":
                flash("Cette action est disponible uniquement pendant une partie en cours.")
                return redirect(url_for("game"))

            listplayers = r.lrange(f"game:{game_code}:players", 0, -1)
            playerplay = r.get(f"game:{game_code}:playerplay")
            if username not in listplayers or not playerplay or int(playerplay) >= len(listplayers):
                flash("Vous ne pouvez plus jouer cette lettre dans cette partie.")
                return redirect(url_for("game"))
            if listplayers[int(playerplay)] != username:
                flash(f"Ce n'est pas votre tour de jouer, c'est le tour de {listplayers[int(playerplay)]}.")
                return redirect(url_for("game"))

            word = r.get(f"game:{game_code}:word")
            available_letters = r.lrange(f"game:{game_code}:{word}", 0, -1)

            if letter in available_letters:
            
                if letter in voyelles:
                    # Les voyelles se "payent" 2500 points, contrairement aux consonnes qui sont gratuites.
                    if int(r.get(f"game:{game_code}:score:{username}") or 0) < 2500:
                        flash(f"Vous n'avez pas assez d'argent pour prendre une voyelle. Il vous faut 2500, vous avez {int(r.get(f'game:{game_code}:score:{username}') or 0)}.")
                        return redirect(url_for("game"))
                    else:
                        occurrences = word.count(letter)  # Nombre de fois où la voyelle apparaît dans le mot
                        current_money = int(r.get(f"game:{game_code}:money") or 100)
                        r.lrem(f"game:{game_code}:{word}", 0, letter)  # Supprime la lettre de la liste des lettres disponibles
                        # La voyelle coûte 2500, mais rapporte la valeur de la roue pour CHAQUE occurrence trouvée.
                        gain = occurrences * current_money
                        r.set(f"game:{game_code}:score:{username}", int(r.get(f"game:{game_code}:score:{username}") or 0) - 2500 + gain, ex=3600)
                        r.set(f'game:{game_code}:money', random.randrange(50, 1000, 50), ex=3600)  # Nouvelle valeur de roue pour le joueur suivant
                        if occurrences == 0:
                            flash(f"La lettre '{letter}' n'est pas dans le mot. Vous avez perdu 2500.")
                            r.set(f"game:{game_code}:playerplay", (int(playerplay) + 1) % len(listplayers), ex=3600)  # Passe au joueur suivant

                else:
                    occurrences = word.count(letter)  # Nombre de fois où la consonne apparaît dans le mot
                    r.lrem(f"game:{game_code}:{word}", 0, letter)  # Supprime la lettre de la liste des lettres disponibles
                    # Une consonne rapporte la valeur de la roue UNE seule fois, quel que soit son nombre d'occurrences.
                    gain = int(r.get(f"game:{game_code}:money") or 100) if occurrences > 0 else 0
                    r.set(f"game:{game_code}:score:{username}", int(r.get(f"game:{game_code}:score:{username}") or 0) + gain, ex=3600)
                    r.set(f'game:{game_code}:money', random.randrange(50, 1000, 50), ex=3600)  # Donne de l'argent aléatoire au joueur suivant
                    if occurrences == 0:
                        flash(f"La lettre '{letter}' n'est pas dans le mot. Vous n'avez rien gagné.")
                        r.set(f"game:{game_code}:playerplay", (int(playerplay) + 1) % len(listplayers), ex=3600)  # Passe au joueur suivant
                return redirect(url_for("game"))
            else:
                flash(f"La lettre '{letter}' n'est pas disponible pour cette partie.")
                return redirect(url_for("game"))
        finally:
            r.eval(
                "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) else return 0 end",
                1,
                transition_key,
                transition_token
            )

    elif request.form.get("text"):
        text = request.form.get("text")
        if not text:
            flash("Texte introuvable")
            return redirect(url_for("game"))

        transition_key = f"game:{game_code}:skip_transition"
        transition_token = secrets.token_hex(16)
        if not r.set(transition_key, transition_token, nx=True, ex=30):
            flash("Une autre action de la partie est en cours. Réessayez dans un instant.")
            return redirect(url_for("game"))

        try:
            if r.get(f"game:{game_code}:status") != "playing":
                flash("Cette action est disponible uniquement pendant une partie en cours.")
                return redirect(url_for("game"))
            listplayers = r.lrange(f"game:{game_code}:players", 0, -1)
            playerplay = r.get(f"game:{game_code}:playerplay")
            if username not in listplayers or not playerplay or int(playerplay) >= len(listplayers):
                flash("Vous ne pouvez plus jouer cette réponse dans cette partie.")
                return redirect(url_for("game"))
            if listplayers[int(playerplay)] != username:
                flash(f"Ce n'est pas votre tour de jouer, c'est le tour de {listplayers[int(playerplay)]}.")
                return redirect(url_for("game"))

            word = r.get(f"game:{game_code}:word")
            if not word:
                flash("Mot de la partie introuvable (partie corrompue ou expirée).")
                return redirect(url_for("game"))
            nb_words = r.get(f"game:{game_code}:nb_words") or 1

            if text.lower() == word.lower():
                r.delete(f"game:{game_code}:skip_votes")
                final_message = f"Félicitations {username}, vous avez deviné le mot '{word}'."
                r.set(f"game:{game_code}:last_event", json.dumps({
                    "player": username,
                    "word": word,
                    "message": final_message,
                    "time": int(time.time())
                }), ex=6)
                # Bonus de manche : valeur de la roue × nombre de lettres encore masquées.
                # Mieux vaut donc jouer les consonnes d'abord et garder les voyelles cachées.
                masked_letters = count_masked_letters(word, r.lrange(f"game:{game_code}:{word}", 0, -1))
                score = masked_letters * int(r.get(f"game:{game_code}:money") or 100)
                r.set(f"game:{game_code}:score:{username}", int(r.get(f"game:{game_code}:score:{username}") or 0) + score, ex=3600)
                r.set(f"game:{game_code}:nb_words", int(nb_words) - 1, ex=3600)
                if int(nb_words) - 1 <= 0:
                    r.set(f"game:{game_code}:status", "finished", ex=3600)
                    flash(f"{final_message} La partie est terminée.")
                    return redirect(url_for("game"))
                flash(f"{final_message} Il reste {int(nb_words) - 1} mots à deviner.")
                load_new_round_word(game_code, word)
                # Le gagnant du mot rejoue en premier sur le mot suivant.
                r.set(f"game:{game_code}:playerplay", listplayers.index(username), ex=3600)
                return redirect(url_for("game"))
            else:
                flash(f"Désolé {username}, ce n'est pas le bon mot.")
                r.set(f"game:{game_code}:playerplay", (int(playerplay) + 1) % len(listplayers), ex=3600)
                return redirect(url_for("game"))
        finally:
            r.eval(
                "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) else return 0 end",
                1,
                transition_key,
                transition_token
            )

    flash("Aucune action valide n'a été fournie.")
    return redirect(url_for("game"))

## Route de debug : n'existe qu'en dev. En prod elle exposerait les pseudos,
## les curseurs et la clé secrète de session à n'importe qui.
if os.getenv("APP_MODE") != "prod":
    @app.route('/getredis')
    def get_redis():
        keys = [k for k in r.keys() if k != SECRET_KEY_REDIS_KEY]
        values = {key: r.get(key) for key in keys}
        return values


## Curseurs des joueurs, façon Figma/Canva : chacun envoie sa position, les autres la récupèrent en direct.
@app.route('/api/cursor', methods=['POST'])
def update_cursor():
    game_code = request.form.get("game_code")
    username = authenticated_username()
    x = request.form.get("x")
    y = request.form.get("y")

    if not game_code or not username or x is None or y is None:
        return {"error": "game_code, x et y sont requis"}, 400
    if username not in r.lrange(f"game:{game_code}:players", 0, -1):
        return {"error": "joueur absent de la partie"}, 403

    try:
        x = max(0.0, min(100.0, float(x)))
        y = max(0.0, min(100.0, float(y)))
    except ValueError:
        return {"error": "x et y doivent être numériques"}, 400

    r.set(f"game:{game_code}:cursor:{username}", json.dumps({"x": x, "y": y}), ex=PLAYER_PRESENCE_TTL)
    r.sadd(cursor_users_key(game_code), username)
    r.expire(cursor_users_key(game_code), 3600)
    return {"ok": True}, 200


@app.route('/api/presence', methods=['POST'])
def update_presence():
    game_code = request.form.get("game_code")
    username = authenticated_username()
    if not game_code or not username:
        return {"error": "game_code et username sont requis"}, 400
    if username not in r.lrange(f"game:{game_code}:players", 0, -1):
        return {"error": "joueur absent de la partie"}, 403

    r.set(player_presence_key(game_code, username), "1", ex=PLAYER_PRESENCE_TTL)
    r.expire(f"game:{game_code}:cursor:{username}", PLAYER_PRESENCE_TTL)
    return {"ok": True}, 200


@app.route('/api/presence/leave', methods=['POST'])
def clear_presence():
    game_code = request.form.get("game_code")
    username = authenticated_username()
    if not game_code or not username:
        return {"error": "game_code et username sont requis"}, 400
    r.delete(player_presence_key(game_code, username), f"game:{game_code}:cursor:{username}")
    r.srem(cursor_users_key(game_code), username)
    return {"ok": True}, 200


@app.route('/api/cursors', methods=['POST'])
def list_cursors():
    game_code = request.form.get("game_code")
    username = authenticated_username()

    if not game_code or not username:
        return {"error": "game_code et username sont requis"}, 400

    listplayers = r.lrange(f"game:{game_code}:players", 0, -1)
    if username not in listplayers:
        return {"error": "joueur absent de la partie"}, 403

    admin_username = r.get(f"game:{game_code}")
    playerplay = r.get(f"game:{game_code}:playerplay")
    current_player_name = None
    if listplayers and playerplay is not None:
        index = int(playerplay) if int(playerplay) < len(listplayers) else 0
        current_player_name = listplayers[index]

    cursors = []
    users_key = cursor_users_key(game_code)
    for cursor_username in r.smembers(users_key):
        cursor_key = f"game:{game_code}:cursor:{cursor_username}"
        raw = r.get(cursor_key)
        if not raw:
            r.srem(users_key, cursor_username)
            continue
        try:
            position = json.loads(raw)
        except (TypeError, ValueError):
            continue
        cursors.append({
            "username": cursor_username,
            "x": position.get("x", 0),
            "y": position.get("y", 0),
            "is_admin": cursor_username == admin_username,
            "is_current": cursor_username == current_player_name
        })

    return {"cursors": cursors}, 200

## API route pour récupérer l'état de la partie ou son hash (utilisée par la page d'attente pour détecter les changements)
@app.route('/api/update/<type>', methods=['POST'])
def api_update(type):
    game_code = request.form.get("game_code")

    if not game_code:
        return {"error": "game_code is required"}, 400

    game_data = {
        "status": r.get(f"game:{game_code}:status"),
        "word": r.get(f"game:{game_code}:word"),
        "playerplay": r.get(f"game:{game_code}:playerplay"),
        "players": r.lrange(f"game:{game_code}:players", 0, -1),
        "money": r.get(f"game:{game_code}:money"),
        "nb_words": r.get(f"game:{game_code}:nb_words")
    }

    # Générer un hash qui correspond à l'état de la partie
    if type == "hash":
        game_hash = generate_game_hash(game_code)
        return {"hash": game_hash}, 200
    else:
        return game_data, 200






if __name__ == "__main__":
    debug_mode = os.getenv("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", debug=debug_mode, threaded=True, port=port)