from flask import Flask, Response, redirect, render_template, make_response, request, url_for, flash
import random
import redis
import os
import pandas
import hashlib
import json


#env
voyelles = ["a", "e", "i", "o", "u", "y"]
consonnes = ["b", "c", "d", "f", "g", "h", "j", "k", "l", "m", "n", "p", "q", "r", "s", "t", "v", "w", "x", "z"]
all_letters = voyelles + consonnes
print("All letters:", all_letters)
port=int(os.getenv("PORT") or 8000)

# Connexion à Redis
r = redis.Redis(
    host=os.getenv("SERVICE_NAME_REDIS") or os.getenv("REDIS_HOST", "localhost"),
    port=int(os.getenv("REDIS_PORT", 6379)),
    decode_responses=True
)

# Fonction pour générer un hash unique de l'état de la partie
def generate_game_hash(game_code):
    data = {
        "status": r.get(f"game:{game_code}:status"),
        "word": r.get(f"game:{game_code}:word"),
        "playerplay": r.get(f"game:{game_code}:playerplay"),
        "players": r.lrange(f"game:{game_code}:players", 0, -1),
        "money": r.get(f"game:{game_code}:money"),
        "nb_words": r.get(f"game:{game_code}:nb_words")
    }
    # Convertir en JSON et hasher en SHA256 (hash court pour éviter les collisions)
    data_str = json.dumps(data, sort_keys=True)  # sort_keys pour un ordre stable
    return hashlib.sha256(data_str.encode()).hexdigest()[:8]  # 8 premiers caractères

app = Flask(__name__)

random.seed()
secure_random = random.SystemRandom()
key = random.randrange(1111111111, 9999999999, 1)
app.secret_key = os.getenv("SECRET_KEY", f"secret_key_{key}")

#pandas connection test
try:
    pd = pandas.read_csv("data/data.csv")
    pd = pd["data"]

    print("Pandas connection successful")

except Exception as e:
    print(f"Error connecting to pandas: {e}")
    pd = None


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


## User logique
@app.route("/")
def index():
    username = request.cookies.get("username")
    return render_template("index.html", code=username, game_code=request.cookies.get("game"))

@app.route("/setusername", methods=["GET", "POST"])
@app.route("/setusername/", methods=["GET", "POST"])
def set_username():
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

    if len(username) < 3 or len(username) > 20:
        flash("Le nom d'utilisateur doit contenir entre 3 et 20 caractères.")
        return redirect(url_for("index"))
    elif r.get(f"user:{username}"):
        flash("Le nom d'utilisateur est déjà pris.")
        return redirect(url_for("index"))

    r.set(f"user:{username}", username, ex=3600)
    resp = redirect(url_for("index"))
    resp.set_cookie('username', username, max_age=3600, secure=request.is_secure, httponly=True)
    return resp

## join game logique
@app.route("/newgame", methods=["POST"])
def newgame():
    username = request.cookies.get("username")

    if not username:
        return redirect(url_for("index"))

    if request.cookies.get("game"):
        return redirect(url_for("game"))

    code = str(random.randrange(1111, 9999))

    word = str(get_available_words())  # Récupère un mot aléatoire depuis le CSV

    # Une partie par code, associée à son utilisateur
    r.set(f"game:{code}", username, ex=3600)
    # Set le mot de la partie pour cette partie
    r.set(f"game:{code}:word", word, ex=3600)
    # Set les lettres pour cette partie
    r.rpush(f"game:{code}:{word}", *all_letters)
    r.expire(f"game:{code}:{word}", 3600)
    # Créer la liste des joueurs pour cette partie
    r.lpush(f"game:{code}:players", username)
    r.expire(f"game:{code}:players", 3600)
    # initialiser le statut de la partie
    r.set(f"game:{code}:status", "waiting", ex=3600)
    r.set(f"game:{code}:play,", "0", ex=3600)
    r.set(f"game:{code}:money", random.randrange(50, 1000, 50), ex=3600)
    r.set(f"game:{code}:nb_words", 5, ex=3600)

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
    username = request.cookies.get("username")

    if not username:
        return redirect(url_for("index"))

    if not game_code:
        game_code = request.form.get("game_code")

    game_exsit = r.get(f"game:{game_code}:status") == "waiting" and r.get(f"game:{game_code}") is not None

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
            r.lpush(f"game:{game_code}:players", username)
        return resp

    flash(f"Partie inconnue : {game_code}")
    return redirect(url_for("index"))

## game logique
@app.route("/waiting", methods=["GET", "POST"])
def waiting():
    game_code = request.cookies.get("game")
    username = request.cookies.get("username")

    if r.get(f"game:{game_code}:status") == "playing":
        return redirect(url_for("game"))

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
                    return redirect(url_for("game"))
                else:
                    flash(f"Impossible de changer le nombre de mots de la partie {game_code}.")
                    return redirect(url_for("waiting"))
            else:
                flash(f"Vous n'êtes pas l'administrateur de la partie {game_code}.")
                return redirect(url_for("waiting"))



    if not game_code or not username:
        flash("Partie ou utilisateur introuvable")
        return redirect(url_for("index"))

    if r.get(f"game:{game_code}:status") == "waiting" and username in r.lrange(f"game:{game_code}:players", 0, -1):
        return render_template("waiting.html",
                               game_code=game_code,
                               players=r.lrange(f"game:{game_code}:players", 0, -1),
                               username=username,
                               admin=(r.get(f"game:{game_code}") == username),
                               nb_words=int(r.get(f"game:{game_code}:nb_words") or 1)
                               )

    flash(f"Partie inconnue : {game_code}")
    return redirect(url_for("index"))


@app.route("/game")
def game():
    game_code = request.cookies.get("game")
    username = request.cookies.get("username")

    if r.get(f"game:{game_code}:status") == "waiting":
        return redirect(url_for("waiting"))

    if not game_code or not username:
        flash("Partie ou utilisateur introuvable")
        return redirect(url_for("index"))

    if r.get(f"game:{game_code}:status") == "playing" and username in r.lrange(f"game:{game_code}:players", 0, -1):
        word = r.get(f"game:{game_code}:word")  # Récupère le mot (string)
        available_letters = r.lrange(f"game:{game_code}:{word}", 0, -1)  # Lettres disponibles

        # Filtre les voyelles/consonnes disponibles
        available_voyelles = [v for v in voyelles if v in available_letters]
        available_consonnes = [c for c in consonnes if c in available_letters]

        # Affiche le mot avec les lettres non disponibles masquées
        display_word = "".join([
            char if char not in available_letters else (char if char not in all_letters else "X")
            for char in word
        ])

        ## Récupère "id" du joueur qui joue actuellement et le contrôle pour savoir si c'est le tour du joueur actuel
        playerplay = r.get(f"game:{game_code}:playerplay")
        listplayers = r.lrange(f"game:{game_code}:players", 0, -1)
        ifplay = False
        if not playerplay:
            r.set(f"game:{game_code}:playerplay", 0, ex=3600)
            playerplay = 0
        elif int(playerplay) >= len(listplayers):
            r.set(f"game:{game_code}:playerplay", 0, ex=3600)
            playerplay = 0
        elif listplayers[int(playerplay)] != username:
            ifplay = False
        else:
            ifplay = True

        listplayers_dysplay = "".join([
            str(r.get(f"game:{game_code}:score:{player}") or 0) +
            f" : {player} | "
            for player in listplayers
        ])

        return render_template("game.html",
                              game_code=game_code,
                              username=username,
                              word=display_word,
                              voyelles=available_voyelles,
                              consonnes=available_consonnes,
                              ifplay=ifplay if 'ifplay' in locals() else False,
                              playerplay=listplayers[int(playerplay)],
                              listplayers=listplayers_dysplay,
                              money=int(r.get(f'game:{game_code}:money') or 0),
                              nb_words=int(r.get(f"game:{game_code}:nb_words") or 1)
                              )

    if r.get(f"game:{game_code}:status") == "finished":
        resp = make_response(render_template("finished.html", game_code=game_code, word=r.get(f"game:{game_code}:word"), listplayers=[{"name": player, "score": r.get(f"game:{game_code}:score:{player}") or 0} for player in r.lrange(f"game:{game_code}:players", 0, -1)]))
        resp.set_cookie('game', '', expires=0)
        return resp

    flash(f"Partie inconnue : {game_code}")
    return redirect(url_for("index"))



@app.route("/guess", methods=["POST"])
def guess():
    game_code = request.form.get("game_code")
    username = request.cookies.get("username")
    if not game_code or not username:
        flash("Partie ou utilisateur introuvable")
        return redirect(url_for("game"))
    playerplay = r.get(f"game:{game_code}:playerplay")
    listplayers = r.lrange(f"game:{game_code}:players", 0, -1)
    if not playerplay:
        flash("Partie inconnue")
        return redirect(url_for("game"))
    if listplayers[int(playerplay)] != username:
        flash(f"Ce n'est pas votre tour de jouer, c'est le tour de {listplayers[int(playerplay)]}.")
        return redirect(url_for("game"))

    if request.form.get("letter"):
        letter = request.form.get("letter")

        if not letter:
            flash("Lettre introuvable")
            return redirect(url_for("game"))

        word = r.get(f"game:{game_code}:word")
        available_letters = r.lrange(f"game:{game_code}:{word}", 0, -1)

        if letter in available_letters:
            
            if letter in voyelles:
                if int(r.get(f"game:{game_code}:score:{username}") or 0) < 2500:
                    flash(f"Vous n'avez pas assez d'argent pour prendre une voyelle. Il vous faut 2500, vous avez {int(r.get(f'game:{game_code}:score:{username}') or 0)}.")
                    return redirect(url_for("game"))
                else:
                    score = word.count(letter)  # Récupère le nombre de lettres du mot pour le score
                    r.lrem(f"game:{game_code}:{word}", 0, letter)  # Supprime la lettre de la liste des lettres disponibles
                    r.set(f"game:{game_code}:score:{username}", int(r.get(f"game:{game_code}:score:{username}") or 0) - 2500, ex=3600)
                    if score == 0:
                        flash(f"La lettre '{letter}' n'est pas dans le mot. Vous avez perdu 2500.")
                        r.set(f"game:{game_code}:playerplay", (int(playerplay) + 1) % len(listplayers), ex=3600)  # Passe au joueur suivant

            else:
                score = word.count(letter)  # Récupère le nombre de lettres du mot pour le score
                r.lrem(f"game:{game_code}:{word}", 0, letter)  # Supprime la lettre de la liste des lettres disponibles
                r.set(f"game:{game_code}:score:{username}", int(r.get(f"game:{game_code}:score:{username}") or 0) + score * int(r.get(f"game:{game_code}:money") or 100), ex=3600)
                r.set(f'game:{game_code}:money', random.randrange(50, 1000, 50), ex=3600)  # Donne de l'argent aléatoire au joueur suivant
                if score == 0:
                    flash(f"La lettre '{letter}' n'est pas dans le mot. Vous n'avez rien gagné.")
                    r.set(f"game:{game_code}:playerplay", (int(playerplay) + 1) % len(listplayers), ex=3600)  # Passe au joueur suivant
            return redirect(url_for("game"))
        else:
            flash(f"La lettre '{letter}' n'est pas disponible pour cette partie.")
            return redirect(url_for("game"))
    elif request.form.get("text"):
        text = request.form.get("text")
        if not text:
            flash("Texte introuvable")
            return redirect(url_for("game"))

        word = r.get(f"game:{game_code}:word")
        if not word:  # ← NOUVEAU : Vérifie que word existe
            flash("Mot de la partie introuvable (partie corrompue ou expirée).")
            return redirect(url_for("game"))
        nb_words = r.get(f"game:{game_code}:nb_words") or 1

        if text.lower() == word.lower():
            r.set(f"game:{game_code}:nb_words", int(nb_words) - 1, ex=3600)
            if int(nb_words) - 1 <= 0:
                r.set(f"game:{game_code}:status", "finished", ex=3600)
                flash(f"Félicitations {username}, vous avez deviné le mot '{word}' ! La partie est terminée.")
                return redirect(url_for("game"))
            else:
                score = word.count(text)  # Récupère le nombre de lettres du mot pour le score
                flash(f"Félicitations {username}, vous avez deviné le mot '{word}' ! Il reste {int(nb_words) - 1} mots à deviner.")
                r.set(f"game:{game_code}:score:{username}", int(r.get(f"game:{game_code}:score:{username}") or 0) + score * int(r.get(f"game:{game_code}:money") or 100), ex=3600)
                # Choisir un nouveau mot aléatoire
                new_word = get_available_words(exclude=word)
                r.set(f"game:{game_code}:word", new_word, ex=3600)
                r.delete(f"game:{game_code}:{word}")  # Supprime l'ancienne liste de lettres
                r.rpush(f"game:{game_code}:{new_word}", *all_letters)  # Crée une nouvelle liste de lettres pour le nouveau mot
                r.expire(f"game:{game_code}:{new_word}", 3600)
                return redirect(url_for("game"))
        else:
            flash(f"Désolé {username}, ce n'est pas le bon mot.")
            r.set(f"game:{game_code}:playerplay", (int(playerplay) + 1) % len(listplayers), ex=3600)  # Passe au joueur suivant
            return redirect(url_for("game"))

    flash("Aucune action valide n'a été fournie.")
    return redirect(url_for("game"))

## debug route to get all redis keys and values
@app.route('/getredis')
def get_redis():
    keys = r.keys()
    values = {key: r.get(key) for key in keys}
    return values

## API route to update game data, can be used for a signal page or other purposes
## if update sinal page api 
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
    app.run(host="0.0.0.0", debug=True, threaded=True, port=port)
