# roudelafortune

## Principle

1) comprendre ton code (pas ia slop)
2) 100% autonome pas de db sur firebase, Supabase, etc
3) pas de connexion a un compte 

## Rebuild du projet

Pour démarrer le projet pour la première fois, ou après un changement de dépendances Python :

```bash
docker compose up --build -d
```

Le service `app` démarre par défaut en mode développement avec un montage du dossier local dans le conteneur. Flask recharge automatiquement les fichiers Python modifiés; les changements de dépendances nécessitent une nouvelle build.

Après la première build, il suffit de lancer :

```bash
docker compose up -d
```

et les changements de code sont pris en compte automatiquement.

Si le port 8000 est déjà utilisé par un autre processus, Docker affichera une erreur de type `address already in use`. Redis reste accessible uniquement sur le réseau interne de Compose et ne publie pas le port 6379 sur l'hôte.

Pour vérifier que l'application est bien disponible :

```bash
curl -I http://127.0.0.1:8000/
```

Le conteneur Flask met quelques secondes à démarrer. Si vous lancez le test trop vite après `docker compose up -d`, vous pouvez obtenir un `Connection reset by peer` ou un refus de connexion. Attendez 5 à 10 secondes, ou utilisez :

```bash
docker compose up -d --wait
curl -I http://127.0.0.1:8000/
```

Le projet est bien accessible avec la réponse `HTTP/1.1 200 OK`.

Pour passer en mode production (uWSGI), lancez :

Configurez une clé stable dans `.env` (ne la commitez pas) :

```bash
SECRET_KEY=une-cle-aleatoire-longue-et-secrete
```

Cette même clé est utilisée par tous les workers uWSGI pour signer les sessions.
La session expire après une heure, comme la réservation du pseudo. En production,
si `SECRET_KEY` est absente, l'application génère une clé partagée et la conserve
dans Redis. Définir `SECRET_KEY` reste recommandé : elle est alors prioritaire.

```bash
APP_MODE=prod docker compose up -d --build
```

Pour arrêter les conteneurs :

```bash
docker compose down
```
