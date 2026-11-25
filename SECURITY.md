# Sécurité de BrickMote

Ce document décrit le modèle de menace, le protocole cryptographique et les limites connues.

## Modèle de menace

BrickMote doit rester sûr face à :

| Adversaire | Protection |
|---|---|
| Écoute passive du réseau (Wi-Fi public, FAI) | chiffrement de bout en bout AES-256-GCM. Nom des postes, mot de passe et contenu ne circulent jamais en clair |
| Serveur relais compromis ou malveillant | le relais ne voit que du chiffré. Il ne peut pas usurper un poste (ID auto-certifiant + épinglage + signatures), ni tester des mots de passe (SPAKE2) |
| Attaquant actif (homme du milieu) | signatures Ed25519 sur la transcription complète, vérification de l'ID, épinglage des clés, code de sécurité affiché des deux côtés, mélange de la clé SPAKE2 dans les clés de session |
| Devinette de mot de passe en ligne | SPAKE2 n'autorise qu'un essai par connexion. Verrouillage par ID (5 échecs / 15 min) et global (20 échecs / 15 min). Une tentative interrompue compte comme un échec |
| Vol d'un code 2FA | TOTP avec fenêtre ±30 s et refus du rejeu |
| Rejeu, réordonnancement, falsification de paquets | nonces à compteur strict par direction : toute anomalie ferme la session |
| Client malveillant connecté | droits vérifiés **côté hôte** à chaque message, touches injectables limitées à une liste blanche, coordonnées bornées, noms de fichiers assainis, tailles bornées |
| Hôte malveillant | paramètres scrypt bornés (pas d'épuisement mémoire), trames bornées, rectangles d'image vérifiés |
| Inondation de demandes | une seule demande interactive à la fois, 3 demandes par minute par ID, limitations par IP sur le port 7070 et sur le relais |

Hors périmètre : un attaquant qui contrôle déjà l'un des deux ordinateurs (logiciel espion, accès administrateur).

## Protocole

### Identité

Chaque poste génère une paire de clés **Ed25519** stockée dans `identity.pem` (droits 0600). Son ID se calcule ainsi :

```
ID = 100000000 + (scrypt(clé_publique, sel="BrickMote-ID-v1", N=2^14, r=8, p=1) mod 900000000)
```

Trouver une clé correspondant à un ID donné demande environ 10⁹ évaluations de scrypt, soit plusieurs années-processeur. L'ID suffit donc à authentifier le poste, et le client épingle en plus la clé complète.

### Poignée de main (viewer = I, hôte = R)

1. `I → R` : clé X25519 éphémère, nonce (en clair)
2. `R → I` : clé X25519 éphémère, nonce (en clair)
3. Clés de poignée de main = HKDF(X25519, sel = SHA-256(transcription)). La suite est chiffrée en AES-256-GCM.
4. `R → I` : identité Ed25519, nom, méthodes d'accès, paramètres scrypt, puis **signature de la transcription**. I vérifie la signature, l'ID et la clé épinglée.
5. `I → R` : identité Ed25519, nom, méthode, message SPAKE2 éventuel, puis **signature de la transcription**.
6. Selon le mode :
   - **interactif** : l'utilisateur de l'hôte voit la demande et le code de sécurité, choisit les droits, accepte ou refuse ;
   - **mot de passe** : échange SPAKE2 (identités liées aux clés Ed25519), confirmation par HMAC dans les deux sens, puis code TOTP éventuel.
7. `R → I` : acceptation avec les droits accordés.
8. Clés de session = HKDF(X25519 ‖ K_SPAKE2, sel = SHA-256(transcription complète)), une clé AES-256 par direction.

Le code de sécurité à 6 chiffres est dérivé du secret partagé et de la transcription : un intermédiaire obtiendrait un code différent de chaque côté.

### Canal chiffré

Chaque message est chiffré en AES-256-GCM avec le nonce `0⁴ ‖ compteur₆₄`. Le compteur est implicite et strictement croissant par direction. Tout message rejoué, supprimé, réordonné ou modifié fait échouer le déchiffrement, et la session est fermée.

### Relais

- TLS 1.3 uniquement. Le certificat est épinglé par empreinte SHA-256, ou validé par une autorité reconnue.
- Enregistrement : le poste signe un défi aléatoire de 32 octets. Le serveur lie définitivement ID et clé (base SQLite), et refuse toute autre clé pour cet ID.
- Mise en relation : chaque partie ouvre une connexion de données et signe `défi ‖ jeton`. Seules les deux clés attendues sont acceptées.
- Le relais connaît les métadonnées : ID, adresses IP, horaires et volume des sessions. Il ne connaît ni le contenu, ni les noms des postes, ni les mots de passe.

## Mots de passe mémorisés (poste client)

- Le mot de passe lui-même n'est jamais enregistré. Seul est conservé le secret dérivé pour **un** poste (scrypt avec le sel de ce poste), c'est-à-dire ce qu'utilise l'échange SPAKE2. Il ne permet de se connecter qu'à ce poste et ne révèle pas le mot de passe, qui peut servir ailleurs.
- Windows : ce secret est chiffré par DPAPI (lié au compte Windows) dans `saved_passwords.json`. Autres systèmes : fichier lisible par le seul propriétaire (0600).
- Si le mot de passe du poste change, la clé mémorisée ne correspond plus : elle est effacée et le mot de passe est redemandé. Le menu ⋮ du poste permet aussi de l'oublier.
- Quand le client doit demander le mot de passe, il interrompt la première connexion juste après avoir authentifié le poste. La personne devant le poste distant ne voit donc aucune demande pendant la saisie.

## Serveur relais par défaut

- Son adresse est intégrée à la compilation depuis des secrets GitHub : elle n'est **jamais dans le code source public**, ni dans les journaux de compilation (GitHub masque les secrets).
- Dans l'exécutable, elle est brouillée (zlib + XOR avec une clé aléatoire différente à chaque compilation) et n'apparaît pas en clair. C'est de l'obscurcissement : une personne déterminée peut toujours retrouver l'adresse en observant le trafic réseau.
- L'application ne l'affiche nulle part : état de connexion, paramètres, messages d'erreur (les erreurs réseau, qui contiennent normalement l'IP, sont reformulées).
- Le certificat du relais est épinglé dans l'exécutable : un faux serveur placé sur le trajet est refusé.
- Un nouveau certificat de relais ne contient plus le nom « BrickMote », que les moteurs de recherche de serveurs indexent. Le certificat d'un relais déjà installé est conservé tant qu'on ne le supprime pas (voir le README).

## Mises à jour automatiques

- La liste des empreintes de chaque release (`SHA256SUMS.txt`) est signée en Ed25519 (`SHA256SUMS.txt.sig`). La clé privée est un secret GitHub. Les clés publiques autorisées sont épinglées dans `brickmote/updater.py`, ce qui permet de changer de clé.
- Un fichier n'est exécuté que s'il figure dans la liste signée **sous le nom exact de la nouvelle version** et que son SHA-256 correspond. Une ancienne release signée ne peut donc pas être republiée comme nouvelle version (pas de retour arrière).
- Les téléchargements se font uniquement en HTTPS, avec une taille maximale. Un fichier refusé est supprimé immédiatement.
- Windows : la tâche planifiée `BrickMote\Update` tourne en SYSTEM au démarrage. Elle télécharge dans le dossier d'installation (Program Files, non modifiable par un utilisateur ordinaire), ce qui empêche de substituer l'installateur entre la vérification et l'exécution.
- Limite : une personne qui obtiendrait à la fois l'accès au dépôt GitHub **et** la possibilité d'exécuter des workflows pourrait utiliser le secret de signature. Protégez le compte GitHub par la double authentification.

## Wake-on-LAN

- Un paquet magique ne fait qu'allumer la machine : il ne donne **aucun accès**. Une connexion reste soumise à l'acceptation, ou au mot de passe et à la 2FA.
- L'adresse MAC de l'hôte, l'adresse de sa box et le port de réveil ne sont transmis qu'aux postes **authentifiés et acceptés**, par le canal chiffré. On peut désactiver ce partage dans Paramètres > Réveil (WoL).
- L'IP publique utilisée par défaut est celle que **votre** serveur relais observe : aucun service tiers du type « quelle est mon IP » n'est contacté. Une adresse de box reçue d'un hôte est validée (IP ou nom DNS uniquement).
- Une adresse MAC reçue est validée strictement, et l'adresse de diffusion annoncée par un hôte est ignorée si ce n'est pas une IPv4.
- Le protocole Wake-on-LAN n'est pas authentifié par nature. Rediriger un port UDP de votre box vers le réseau local permet à n'importe qui sur Internet d'allumer le PC (sans pouvoir s'y connecter). Le mot de passe SecureOn, si la carte réseau le gère, limite ce risque.

## Stockage local

| Fichier | Contenu | Protection |
|---|---|---|
| `identity.pem` | clé privée Ed25519 | 0600 |
| `settings.json` | réglages, dérivée scrypt du mot de passe, secret TOTP | 0600, écriture atomique |
| `known_hosts.json` | clés épinglées des postes distants | 0600 |
| `audit.log` | journal des connexions (acceptées, refusées, échecs) | 0600 |
| `thumbnails/` | miniature (480×270) de l'écran des postes distants, affichée dans « Récents » | 0600, désactivable et effaçable dans Paramètres > Général |

Emplacements : `%APPDATA%\BrickMote` (Windows), `~/Library/Application Support/BrickMote` (macOS), `~/.config/brickmote` (Linux).

## Limites connues et recommandations

- La dérivée scrypt stockée permet de se connecter **à ce poste** sans connaître le mot de passe. Elle est protégée par les droits du fichier, mais quelqu'un qui peut lire ce fichier a de toute façon déjà accès à la machine. Un PAKE asymétrique (OPAQUE) supprimerait cette limite.
- Lors d'une première connexion **par IP**, l'utilisateur doit vérifier l'ID affiché (l'application le demande). Par ID, la vérification est automatique.
- Sous Windows et macOS, la clé privée est protégée par les droits du compte utilisateur, pas par le trousseau du système.
- Les exécutables ne sont pas signés. Pour une diffusion large, signez-les (Authenticode sous Windows, notarisation Apple sous macOS).
- Activez la **liste blanche** et la **2FA** sur tout poste exposé en accès sans surveillance.
