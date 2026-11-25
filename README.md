# BrickMote

Logiciel de **bureau à distance** dans l'esprit d'AnyDesk : un ID à 9 chiffres par poste, connexion en un clic, contrôle de la souris et du clavier, presse-papiers, transfert de fichiers et chat. BrickMote est **chiffré de bout en bout**, **auto-hébergeable** et **sans aucune limite** : pas de compte, pas de licence, pas de limite de durée ni de nombre de sessions, et pas de serveur imposé.

> BrickMote est un logiciel indépendant. Il utilise les **mêmes ports** qu'AnyDesk (pour passer les mêmes pare-feu) mais son propre protocole, il n'est donc pas compatible avec les clients AnyDesk.

## Fonctionnalités

| | |
|---|---|
| Connexion | par **ID** (via votre serveur relais), par **adresse IP** directe, ou par ID sur le **réseau local** (découverte automatique) |
| Contrôle | souris, molette, clavier (raccourcis compris), Ctrl+Alt+Suppr, mode lecture seule |
| Affichage | multi-écrans, 3 niveaux de qualité, envoi incrémental (seules les zones modifiées), contrôle de flux anti-latence |
| Échanges | presse-papiers bidirectionnel, transfert de fichiers dans les deux sens (vérifié SHA-256), chat |
| Accès | **interactif** (une personne accepte, avec un code de sécurité) ou **sans surveillance** (mot de passe + double authentification TOTP en option) |
| Contrôle d'accès | liste blanche d'ID, liste de postes bloqués, droits modifiables en direct pendant la session, journal de sécurité |
| Postes | onglets Récents, Favoris et Réseau local, en grandes vignettes (avec aperçu du dernier écran vu), petites vignettes ou liste ; étoile pour épingler, renommage, oubli d'un poste |
| Réveil | **Wake-on-LAN** : réveil d'un PC éteint ou en veille, sur le port UDP de votre choix (9 par défaut), en réseau local ou depuis Internet ; l'adresse MAC est apprise automatiquement pendant une session |
| Transparence | un panneau « Session à distance en cours » reste affiché sur l'hôte tant que quelqu'un est connecté : **pas de mode caché** |

## Ports (identiques à AnyDesk)

| Port | Protocole | Usage |
|---|---|---|
| **6568** | TCP | serveur relais (port principal) |
| **443**, **80** | TCP | serveur relais (ports de secours si 6568 est filtré) |
| **7070** | TCP | connexions directes (réseau local ou redirection de port) |
| **50001** | UDP | découverte des postes sur le réseau local |

Les clients n'ont besoin que de connexions **sortantes** vers le relais. Le port 7070 entrant n'est utile que pour les connexions directes.

## Sécurité en bref

La sécurité est la priorité du projet. Le détail (modèle de menace, protocole) est dans [SECURITY.md](SECURITY.md).

- **Chiffrement de bout en bout** : X25519 (échange de clés éphémères, confidentialité persistante), Ed25519 (identité des postes), AES-256-GCM (données), HKDF-SHA256. **Le serveur relais ne voit jamais le contenu des sessions** et ne peut pas le modifier.
- **ID auto-certifiant** : l'ID à 9 chiffres est dérivé de la clé publique du poste (scrypt). Se faire passer pour un ID demanderait des années de calcul. La clé est ensuite **épinglée** après la première connexion.
- **Mot de passe jamais transmis** : l'accès sans surveillance utilise **SPAKE2** (échange de clés authentifié par mot de passe). Le mot de passe ne circule pas, même haché, et une écoute ne permet pas de le deviner hors ligne. Seule une dérivée scrypt salée est stockée.
- **Anti-force brute** : 5 échecs bloquent l'ID concerné pendant 15 min, et 20 échecs au total suspendent l'accès par mot de passe pendant 5 min. Une tentative interrompue compte comme un échec.
- **Double authentification** TOTP (Google Authenticator, Aegis, 2FAS…) avec protection contre le rejeu.
- **Code de sécurité** à 6 chiffres affiché des deux côtés, pour détecter une interception.
- **Relais en TLS 1.3** avec épinglage du certificat. Chaque poste s'enregistre en signant un défi, et un tiers qui intercepte un jeton de session ne peut pas s'y brancher.
- **Robustesse** : tailles de trames bornées, toutes les entrées réseau validées, noms de fichiers reçus assainis (pas de `../`, pas d'écrasement), touches injectables limitées à une liste blanche, et touches relâchées automatiquement en fin de session.

## Installation

### Exécutables prêts à l'emploi

Le workflow GitHub Actions (`.github/workflows/build.yml`) lance les tests puis produit `BrickMote.exe` (Windows), `BrickMote.app` (macOS) et `BrickMote` (Linux) à chaque push. Les exécutables se téléchargent dans l'onglet **Actions** du dépôt, section *Artifacts*.

### Depuis les sources (Windows, macOS, Linux)

Prérequis : Python 3.10 ou plus récent.

```bash
python -m venv .venv
```

```bash
.venv/bin/pip install -r requirements.txt
```

```bash
.venv/bin/python -m brickmote
```

(Sous Windows, remplacez `.venv/bin/` par `.venv\Scripts\`.)

### Construire l'exécutable soi-même

```bash
pip install -r requirements-dev.txt
```

```bash
python packaging/make_icon.py
```

```bash
pyinstaller --noconfirm packaging/brickmote.spec
```

Le résultat se trouve dans `dist/`.

### Particularités par système

- **Windows** : au premier lancement, autorisez BrickMote dans le pare-feu (réseau privé) pour les connexions directes sur le port 7070.
- **macOS** : autorisez BrickMote dans *Réglages Système > Confidentialité et sécurité*, rubriques **Enregistrement de l'écran** et **Accessibilité**.
- **Linux** : une session **X11** est nécessaire pour être contrôlé (Wayland bloque la capture et l'injection). Choisissez « Xorg » à l'écran de connexion.

## Utilisation

1. Lancez BrickMote sur les deux ordinateurs.
2. Sur le poste à contrôler, notez **l'ID** affiché à gauche (ex. `870 538 076`).
3. Sur votre poste, saisissez cet ID (ou l'adresse IP du poste) puis cliquez sur **Se connecter**.
4. Sur le poste distant, une fenêtre affiche la demande, **le code de sécurité** et les droits demandés. Vérifiez que le code est identique des deux côtés, puis cliquez sur **Accepter**.

### Accès sans surveillance (serveurs, PC personnels)

Sur le poste à contrôler, ouvrez **Paramètres > Sécurité** :

1. Cochez **Activer l'accès sans surveillance** et définissez un mot de passe robuste (10 caractères minimum, 3 types de caractères).
2. Activez si possible la **double authentification (2FA)** : scannez le QR code avec votre application d'authentification.
3. Dans **Contrôle d'accès**, activez la **liste blanche** avec les ID de vos propres postes : ce sont alors les seuls autorisés à se connecter.

Côté client, cochez **Accès sans surveillance** sous le champ d'ID et saisissez le mot de passe.

### Réveiller un PC (Wake-on-LAN), via la box comme une appli de téléphone

BrickMote envoie le « paquet magique » à **l'adresse publique de votre box** (IP ou nom DynDNS), sur le **port UDP de votre choix**. La box le transmet au PC. Le paquet part aussi en diffusion sur le réseau local, ce qui permet de réveiller le PC aussi bien depuis chez vous que depuis l'extérieur.

1. **Sur le PC à réveiller**, dans **Paramètres > Réveil (WoL)** : indiquez l'adresse de la box (de préférence un nom DynDNS) et le port UDP redirigé (9 par défaut). Si vous laissez l'adresse vide, BrickMote utilise l'IP publique vue par votre serveur relais. L'adresse MAC du PC est affichée au même endroit.
2. **Dans la box** : redirigez ce port UDP vers le PC (ou vers l'adresse de diffusion du réseau, ex. 192.168.1.255), ou activez la fonction « Wake on LAN » de la box si elle en a une.
3. **Sur le PC** : activez « Wake on LAN » dans le BIOS/UEFI et, sous Windows, dans *Gestionnaire de périphériques > carte réseau > Gestion de l'alimentation*. Si le PC est éteint (pas en veille), désactivez aussi le démarrage rapide de Windows. Préférez un câble Ethernet.

Après une première session, le poste client a reçu (par le canal chiffré) l'adresse MAC, l'adresse de la box et le port. Le bouton ⏻ de la vignette suffit alors, et BrickMote propose aussi « Réveiller ce PC » quand une connexion échoue. Le bouton **Réveiller un PC**, sous la barre de connexion, permet une saisie manuelle : MAC, box, port, mot de passe SecureOn facultatif.

## Serveur relais (connexion par ID via Internet)

Sans relais, BrickMote fonctionne sur le réseau local (par ID ou par IP) et par IP directe. Pour joindre un poste par son ID à travers Internet et les box ou NAT, hébergez votre propre relais sur un serveur (VPS, Raspberry Pi avec redirection de ports…).

### Avec Docker

Modifiez `--hostname` dans `deploy/docker-compose.yml`, puis lancez :

```bash
docker compose -f deploy/docker-compose.yml up -d
```

Récupérez ensuite l'empreinte du certificat :

```bash
docker compose -f deploy/docker-compose.yml logs relay
```

### Sans Docker

```bash
pip install -r requirements-relay.txt
```

```bash
python -m brickmote.server --ports 6568,443,80 --hostname relais.mondomaine.fr --data-dir ./relay-data
```

Un service systemd durci est fourni dans `deploy/brickmote-relay.service`. Ouvrez les ports TCP **6568**, **443** et **80** dans le pare-feu du serveur. Si 443 et 80 sont déjà pris, `--ports 6568` suffit.

### Configurer les clients

Au démarrage, le serveur affiche une **empreinte SHA-256** de son certificat. Sur chaque poste, dans **Paramètres > Réseau**, renseignez :

- **Adresse** : le nom DNS ou l'IP du serveur ;
- **Ports** : `6568,443,80` ;
- **Empreinte TLS** : l'empreinte affichée par le serveur (épinglage du certificat).

Vous pouvez aussi fournir un certificat reconnu (Let's Encrypt) avec `--cert` et `--key`, et laisser l'empreinte vide.

## Tests

```bash
pip install -r requirements-dev.txt
```

```bash
python -m pytest -q
```

Les tests couvrent la cryptographie (vecteurs RFC 4226/6238, falsification, rejeu, trames géantes), la poignée de main (acceptation, refus, mauvais mot de passe et verrouillage, 2FA, liste blanche, usurpation d'identité, homme du milieu), le relais (épinglage, vol de jeton) et des sessions complètes (flux d'écran, contrôle, droits révoqués en direct, fichiers piégés).

## Limites connues

- Pas de son, ni d'impression à distance.
- Windows : l'écran de connexion, les invites UAC et le vrai Ctrl+Alt+Suppr demandent un service système, qui n'existe pas encore. Lancer BrickMote en administrateur permet d'interagir avec les fenêtres élevées.
- L'image est encodée en JPEG par tuiles : idéal pour la bureautique et l'administration, moins pour la vidéo plein écran.
- Linux : Wayland n'est pas pris en charge (X11 requis côté hôte).

## Structure du code

```
brickmote/
  crypto/       identité Ed25519, poignée de main E2E, canal AES-GCM, mot de passe & TOTP
  net/          trames, TLS, client relais, découverte LAN
  host/         capture d'écran, injection clavier/souris, politique d'accès, session hôte
  viewer/       session côté client
  server/       serveur relais
  gui/          interface Qt (PySide6)
  core.py       moteur (écoute 7070, relais, connexions)
  session.py    chat, presse-papiers, transfert de fichiers
deploy/         Docker, docker-compose, systemd pour le relais
packaging/      recette PyInstaller et icônes
tests/          suite de tests
```

## Usage responsable

N'utilisez BrickMote que sur des ordinateurs qui vous appartiennent, ou avec l'accord explicite de leur utilisateur. Ne communiquez jamais votre ID et n'acceptez jamais une connexion à la demande d'un inconnu (arnaques au faux support technique).
