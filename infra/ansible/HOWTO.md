# Ansible — How-To

## Vue d'ensemble

Ce répertoire contient la configuration Ansible pour configurer les hôtes Docker et déployer les stacks de services.

```
ansible/
├── ansible.cfg
├── deploy.sh
├── playbooks/
│   └── docker-host.yml
├── inventories/
│   ├── test/
│   │   ├── hosts.ini
│   │   └── group_vars/
│   │       ├── all.yml
│   │       └── docker_hosts.yml
│   └── prod/
│       ├── hosts.ini
│       └── group_vars/
│           ├── all.yml
│           └── docker_hosts.yml
└── roles/
    ├── common/
    ├── docker/
    └── compose_deploy/
```

---

## Déploiement

### Prérequis

- [`ansible`](https://docs.ansible.com/ansible/latest/installation_guide/index.html) installé localement
- [`bw`](https://bitwarden.com/help/cli/) (Bitwarden CLI) installé et configuré
- [`bws`](https://bitwarden.com/help/secrets-manager-cli/) (Bitwarden Secrets Manager CLI) installé
- [`jq`](https://stedolan.github.io/jq/) installé
- Accès SSH à la machine cible (clé publique déposée)

### Commande

```bash
# Déployer tout (docker host + haos) sur un environnement
./deploy.sh test
./deploy.sh prod

# Déployer uniquement le docker host
./deploy.sh test --limit docker_hosts
./deploy.sh prod --limit docker_hosts

# Déployer uniquement HAOS
./deploy.sh test --limit haos
./deploy.sh prod --limit haos

# Autres options utiles
./deploy.sh test --check          # dry-run, aucune modification
./deploy.sh test --diff           # afficher les diffs de fichiers
```

### Ce que fait deploy.sh

1. Vérifie que `bw`, `bws`, `jq` et `ansible-playbook` sont disponibles
2. S'authentifie à Bitwarden CLI (via API key ou session existante)
3. Déverrouille le vault si nécessaire
4. Récupère le token machine `bws` depuis le vault (`bws_machine_token`)
5. Charge les secrets dans l'environnement :
   - `MQTT_PASSWORD` (depuis le secret nommé `MQTT_PASSWORD`)
   - `Z2M_NETWORK_KEY` (depuis le secret nommé `Z2M_NETWORK_KEY_JSON`)
   - `CF_DNS_API_TOKEN` (depuis le secret nommé `CF_DNS_API_TOKEN`)
6. Lance `ansible-playbook` avec l'inventaire de l'environnement

---

## Secrets

Les secrets ne sont jamais stockés dans le repo. Ils sont injectés au moment du déploiement.

| Variable Ansible      | Source                              | Secret Bitwarden        |
|-----------------------|-------------------------------------|-------------------------|
| `mqtt_password`       | `lookup('env', 'MQTT_PASSWORD')`    | `MQTT_PASSWORD`         |
| `z2m_network_key`     | `lookup('env', 'Z2M_NETWORK_KEY')`  | `Z2M_NETWORK_KEY_JSON`  |
| `cf_api_token`        | `lookup('env', 'CF_DNS_API_TOKEN')` | `CF_DNS_API_TOKEN`      |
| `restic_backup_rest_password` | `lookup('env', 'RESTIC_REST_PASSWORD')` | `restic_rest_password_<env>` (facultatif) |
| `restic_backup_password`   | `lookup('env', 'RESTIC_REPO_PASSWORD')`  | `restic_password_<env>` (facultatif)   |

Le token machine `bws` est lui-même stocké dans le vault Bitwarden sous le nom `bws_machine_token`.

Les secrets partagés entre test et prod (même Bitwarden secret, deux machines distinctes) sont acceptables pour un homelab. Pour les séparer, utiliser les variables `MQTT_SECRET_REF`, `Z2M_NETWORK_KEY_SECRET_REF`, `CF_DNS_API_TOKEN_SECRET_REF` dans l'environnement avant d'appeler `deploy.sh`.

---

## Différences entre environnements

Toute la logique est partagée. Seules les variables diffèrent :

| Variable              | test                                  | prod                              |
|-----------------------|---------------------------------------|-----------------------------------|
| `ansible_host`        | `docker.test.reminat.com`             | `docker.reminat.com`              |
| `smlight_serial_url`  | `tcp://smlight.test.reminat.com:6638` | `tcp://smlight.reminat.com:6638`  |
| `domain_base`         | `test.reminat.com`                    | `reminat.com`                     |

Les volumes Docker sont isolés par VM : `/srv/docker/core` existe indépendamment sur chaque machine.

---

## GitHub Actions — Self-hosted runner

Les runners auto-hébergés (`docker-test-01` pour `test`, `docker-prod` pour `prod`)
exécutent les déploiements :

| Déclencheur | Effet |
|---|---|
| Push sur `feature/**` touchant des fichiers applicatifs (`services/docker`, `services/ha`, `infra/ansible` hors NAS, hors `*.md`) | deploy sur `test` |
| Push sur `feature/**` ne touchant que le NAS ou la documentation | rien |
| Merge / push sur `main` | **rien** (seul le scan Gitleaks tourne) |
| *Actions → Deploy → Run workflow*, `test` | deploy sur `test` |
| *Actions → Deploy → Run workflow* depuis `main`, `prod` | deploy sur `prod` après **approbation** de l'environnement `prod` ; refusé depuis une autre branche |
| *Actions → NAS — Restic maintenance* | NAS (`test` ou `prod`, prod depuis `main` uniquement), après approbation pour `prod` |

Chaque déploiement (applicatif ou NAS) commence par une analyse Gitleaks de **tout
l'historique** (`.github/workflows/gitleaks-full.yml`) ; une fuite bloque le déploiement.

### Réglages GitHub attendus (dépôt public)

À vérifier dans *Settings* ; ils ne sont pas versionnés.

- **Environments → prod** : *Required reviewers* = `reminat` ; *Deployment branches and
  tags* = *Selected branches* `main` ; décocher *Allow administrators to bypass configured
  protection rules*.
- **Rules → Rulesets** : ruleset actif sur `main` (*Default branch*) : *Restrict
  deletions*, *Block force pushes*, *Require a pull request before merging* (0 approbation
  requise suffit pour un compte seul), *Require status checks to pass* : `Gitleaks`
  (workflow *Secrets Scan*), *Require branches to be up to date*. Aucun contournement.
- Ces protections (approbation d'environnement, rulesets) ne sont pas disponibles sur
  un dépôt **privé** avec l'offre GitHub Free : repasser le dépôt en privé les
  désactiverait sans avertissement.

### Prérequis (une seule fois)

**1. Clé SSH dédiée**

```bash
ssh-keygen -t ed25519 -C "github-runner" -f ~/.ssh/id_github_runner -N ""
```

Ajouter la clé publique dans `~/.ssh/authorized_keys` sur :
- `remi@docker.reminat.com`
- `remi@docker.test.reminat.com`
- `root@haos.reminat.com` (haos-prod)
- `root@haos.test.reminat.com` (haos-test)

**2. Secrets GitHub** (Settings → Secrets and variables → Actions → Repository secrets)

| Secret | Valeur |
|--------|--------|
| `ANSIBLE_SSH_KEY` | Contenu de `~/.ssh/id_github_runner` (clé privée) |
| `BWS_ACCESS_TOKEN` | Token machine Bitwarden Secrets Manager |

**3. Installer le runner sur docker-prod**

Générer un token depuis GitHub : Settings → Actions → Runners → New self-hosted runner → copier le token affiché.

```bash
# Depuis le poste local, passer le token en extra-var
cd infra/ansible
./deploy.sh prod -e github_runner_registration_token=TOKEN_ICI
```

Le runner est enregistré, installé comme service systemd et démarré automatiquement. Le token n'est plus nécessaire après ça.

**Vérifier que le runner tourne :**

```bash
ssh remi@docker.reminat.com
systemctl status "actions.runner.remi-remidom.docker-prod"
```

Il doit aussi apparaître dans GitHub : Settings → Actions → Runners.

---

## Rôles

### `common`
- Définit le timezone
- Installe les packages de base (`curl`, `git`, `jq`, etc.)

### `docker`
- Ajoute le dépôt APT officiel Docker
- Installe Docker CE, CLI, containerd, buildx, compose-plugin
- Active et démarre le service Docker
- Ajoute les utilisateurs au groupe `docker`
- Crée le répertoire de données (`/srv/docker`)

### `compose_deploy`
- Synchronise les projets Docker Compose sur l'hôte (rsync avec `--checksum`)
- Rend les templates :
  - `.env.j2` → `.env` (TZ, DOMAIN_BASE, CF_DNS_API_TOKEN)
  - `traefik/traefik.yml.j2` → `traefik/traefik.yml`
  - `zigbee2mqtt/data/configuration.yaml.j2` → `configuration.yaml`
- Crée `traefik/acme/acme.json` avec les permissions `600` (requis par Traefik)
- Configure les permissions (Mosquitto, Node-RED)
- Génère le fichier de mots de passe Mosquitto
- Lance `docker compose pull` puis `docker compose up -d`
- Reload Mosquitto

---

## HAOS (Home Assistant OS)

### Prérequis : bootstrap SSH

HAOS ne supporte pas cloud-init. Avant de pouvoir déployer via Ansible, les clés SSH doivent être injectées via le script dédié :

```bash
cd infra/proxmox/scripts
./bootstrap-haos.sh test   # ou prod
```

Le script utilise Bitwarden et le QEMU guest agent — aucune action manuelle sur la console Proxmox. Voir `infra/terraform/HOW-TO.md` pour les détails.

> L'utilisateur SSH sur HAOS est `root` (pas `remi`).

### Inventaires HAOS

Les VMs HAOS sont déclarées dans un groupe séparé `haos` dans chaque inventaire :

| Variable        | test                        | prod                   |
|-----------------|-----------------------------|------------------------|
| `ansible_host`  | `haos.test.reminat.com`     | `haos.reminat.com`     |
| `ansible_user`  | `root`                      | `root`                 |

### Playbook haos.yml

Le playbook `playbooks/haos.yml` applique le rôle `haos_config` sur le groupe `haos`.

Il déploie uniquement `configuration.yaml` (rendu depuis un template Jinja2) et recharge Home Assistant.

```bash
# Déployer la config HAOS sur test
./deploy.sh test --limit haos

# Déployer la config HAOS sur prod
./deploy.sh prod --limit haos
```

### Workflow test → prod

```
1. Modifier services/ha/config/configuration.yaml.j2
2. ./deploy.sh test --limit haos   → valider sur test
3. ./deploy.sh prod --limit haos   → promouvoir en prod
```

---

## Traefik

Traefik est le reverse proxy de la stack. Il gère :
- La redirection HTTP → HTTPS automatique
- Les certificats TLS via Let's Encrypt + DNS challenge Cloudflare
- Le routing vers les services selon le domaine

### Domaines exposés

| Service         | test                            | prod                      |
|-----------------|---------------------------------|---------------------------|
| Traefik         | `traefik.test.reminat.com`      | `traefik.reminat.com`     |
| Zigbee2MQTT     | `zigbee2mqtt.test.reminat.com`  | `zigbee2mqtt.reminat.com` |
| Node-RED        | `nodered.test.reminat.com`      | `nodered.reminat.com`     |
| Portainer       | `portainer.test.reminat.com`    | `portainer.reminat.com`   |
| Home Assistant  | `haos.test.reminat.com`         | `haos.reminat.com`        |
| Proxmox (LAN/VPN uniquement) | — | `proxmox.reminat.com` |

### Labels Docker Compose

Les labels sur chaque service indiquent à Traefik :
- `traefik.enable=true` — expose ce container (nécessaire car `exposedByDefault: false`)
- `routers.<name>.rule` — règle de routage basée sur le domaine
- `routers.<name>.tls.certresolver` — resolver ACME à utiliser pour le certificat
- `services.<name>.loadbalancer.server.port` — port interne du container

### Token API Cloudflare

Prérequis : le domaine `reminat.com` doit être géré par les DNS Cloudflare.

1. Dashboard Cloudflare → **My Profile** → **API Tokens** → **Create Token**
2. Template : **Edit zone DNS**
3. Permissions : `Zone / DNS / Edit`
4. Zone Resources : `Include / Specific zone / reminat.com`
5. Créer le token et l'ajouter dans Bitwarden Secrets Manager sous le nom `CF_DNS_API_TOKEN`

### Ports à ouvrir sur le firewall

- `80/tcp` — HTTP (redirigé vers HTTPS par Traefik)
- `443/tcp` — HTTPS

---

## Sauvegarde Restic des hôtes Docker

Rôle `restic_backup` (playbook `docker-host.yml`, tag `restic_backup`), désactivé par
défaut, activé en prod (`group_vars/docker_hosts.yml`). Il sauvegarde `/srv/docker/core`
vers le REST Server du NAS (repository append-only : le client ne peut rien supprimer).

| Élément | Valeur (prod) |
|---|---|
| Hôte, chemin, tags | `docker-prod-01`, `/srv/docker/core`, `core,prod` (exclu : `mosquitto/log`) |
| REST Server | `https://restic.reminat.com`, utilisateur `docker-prod`, repository `/docker-prod/` (`group_vars`) ; `--private-repos` impose que le chemin commence par le nom d'utilisateur |
| Mot de passe REST | secret `restic_rest_password_prod` (même convention que `restic_rest_password_test`) |
| Mot de passe du repository | secret `restic_password_prod` (à conserver aussi hors de l'hôte : sans lui, aucune restauration) |
| URL restic | assemblée par Ansible dans `/etc/restic/core.env`, mot de passe REST encodé |
| Image | `restic/restic@sha256:…` (même digest que la maintenance du NAS) |
| Fichiers sur l'hôte | `/etc/restic/core.env`, `/etc/restic/core.password` (root, 0600), cache `/var/cache/restic/core` |
| Commande | `sudo /usr/local/sbin/restic-core <commande restic>` (données en lecture seule) |
| Service, timer | `restic-backup-core.service` (oneshot), `restic-backup-core.timer` (02:30, activé par `restic_backup_timer_enabled`) |

### Cohérence à chaud, service par service

Vérifié dans le code source des versions déployées. « Atomique » : le fichier est écrit à
côté puis renommé ; une copie voit l'ancienne ou la nouvelle version complète, jamais un
mélange.

| Service | Fichiers | Écriture | Sauvegarde | À la restauration |
|---|---|---|---|---|
| Mosquitto 2.0.22 | `mosquitto/data/mosquitto.db` | atomique (`.new` + `fsync` + `rename`, `persist_write.c`) ; enregistré toutes les 30 min (`autosave_interval` par défaut) et à l'arrêt | à chaud | messages retenus et sessions persistantes à l'état du dernier enregistrement (jusqu'à 30 min avant la sauvegarde) |
| | `mosquitto/config/*` (dont `passwords`) | statique, modifié au déploiement | à chaud (éviter un déploiement simultané) | identique |
| Zigbee2MQTT 2.13.0 | `database.db` | atomique (`.tmp` + `fsync` + `rename`, zigbee-herdsman 10.8.0), toutes les 5 min et sur événement | à chaud | appareils connus à l'état du dernier enregistrement |
| | `coordinator_backup.json` | atomique (`.tmp` + `rename`), quotidien | à chaud | sauvegarde du coordinateur (même adaptateur conseillé) |
| | `state.json` | **non atomique** (`writeFileSync` toutes les 5 min) | à chaud, risque faible de copie tronquée | état des appareils seulement (simple cache) : s'il est illisible, Zigbee2MQTT repart sans, et les appareils republient leur état |
| | `configuration.yaml` | rendu par Ansible (clé réseau dans Bitwarden) | à chaud | identique, ou redéployé par Ansible |
| Node-RED 4.1.8 | `flows.json`, `flows_cred.json`, `.config.*.json` | atomique (`.$$$` + `fsync` + `rename`, `localfilesystem/util.js`) | à chaud | flux au dernier déploiement ; identifiants déchiffrables avec la clé du même dossier |
| | `node_modules/` | statique | à chaud | identique |
| Portainer 2.39.1 | `portainer.db` | **BoltDB modifiée sur place** (mmap), écritures périodiques ; verrou exclusif empêchant une lecture cohérente par un autre processus | **arrêt pendant la sauvegarde** | état à l'heure de la sauvegarde |
| Traefik | `acme/acme.json` | écriture directe, rare (renouvellements) | à chaud, risque faible | au pire, certificats redemandés automatiquement (challenge DNS) |

Seul **Portainer** est donc arrêté (`restic_backup_stop_services: [portainer]`), pour la
durée de la commande `restic backup` : quelques secondes à quelques dizaines de secondes
pour 81 Mo (la première sauvegarde lit tout, les suivantes seulement les changements).
Portainer n'est qu'une interface d'administration : aucun effet sur la domotique. Il est
redémarré même si la sauvegarde échoue.

Limites : chaque fichier est cohérent, mais les fichiers de services différents sont
capturés à quelques secondes d'intervalle ; les données écrites en mémoire et pas encore
enregistrées (Mosquitto, Zigbee2MQTT) ne sont pas dans la sauvegarde.

### Utilisateur REST (NAS, une fois)

Le REST Server tourne avec `--append-only --private-repos` ; ses utilisateurs sont dans
`PASSWORD_FILE=/config/.htpasswd` (conteneur `rest-server`). Créer `docker-prod` avec le
mot de passe du secret `restic_rest_password_prod`, saisi à l'invite (sans écho) :
```sh
N=$(sudo docker ps --format '{{.Names}}' | grep -i rest)
sudo docker exec "$N" cut -d: -f1 /config/.htpasswd            # utilisateurs existants (noms seulement)
sudo docker exec -it "$N" create_user docker-prod              # mot de passe demandé à l'invite
```
Si le REST Server ne prend pas le nouvel utilisateur en compte (401), le redémarrer en
dehors des sauvegardes. Vérification depuis `docker-prod-01`, sans restic (mot de passe
demandé par `curl`) : `curl -s -o /dev/null -w '%{http_code}\n' -u docker-prod
https://restic.reminat.com/docker-prod/config` ; attendu **404** (authentifié, repository
absent) ; 401 = identifiants refusés ; 403 = chemin refusé.

### Première mise en service

L'existence du repository est vérifiée **séparément** de l'initialisation :
```sh
sudo /usr/local/sbin/restic-core status
```
| Résultat | Code | Signification | Suite |
|---|---|---|---|
| `PRÉSENT` | 0 | repository existant, mot de passe accepté | pas d'initialisation |
| `ABSENT` | 10 | aucun repository à cette adresse | `init` autorisé |
| `ERREUR` | 12 | repository existant, mot de passe du repository refusé | s'arrêter, vérifier `restic_password_prod` |
| `INCERTAIN` | autre | réseau, authentification REST, droits… (erreur affichée) | s'arrêter, analyser l'erreur |

`init` refait lui-même cette vérification et refuse (code 2) si l'absence n'est pas
confirmée (code 10). Il reste une commande explicite, jamais lancée automatiquement :
```sh
sudo /usr/local/sbin/restic-core init                       # seulement après « ABSENT »
sudo systemctl start restic-backup-core.service             # Portainer arrêté le temps de la copie
systemctl show -p Result,ExecMainStatus restic-backup-core.service
journalctl -u restic-backup-core.service --since "-15min" --no-pager | tail -20
sudo docker compose --project-directory /srv/docker/core ps
sudo /usr/local/sbin/restic-core snapshots
sudo /usr/local/sbin/restic-core check
```
Planification : `restic_backup_timer_enabled: true` dans `group_vars/docker_hosts.yml`,
puis déploiement.

### Restauration

Toujours dans un dossier vide, jamais par-dessus les données :
```sh
sudo mkdir /var/tmp/restauration
sudo /usr/local/sbin/restic-core restore-to /var/tmp/restauration latest   # ou un identifiant de snapshot
R=/var/tmp/restauration/srv/docker/core
```

- **Test de restauration** (sans rien remplacer) :
  `sudo diff -rq --exclude=log /srv/docker/core "$R"` ; seules des différences sur des
  fichiers vivants (`mosquitto.db`, `state.json`, `database.db`…) sont attendues.
- **Restaurer un service** (exemple Node-RED) :
  ```sh
  sudo docker compose --project-directory /srv/docker/core stop nodered
  sudo rsync -aHAX --delete "$R/nodered/" /srv/docker/core/nodered/
  sudo docker compose --project-directory /srv/docker/core start nodered
  ```
  Même schéma pour `mosquitto`, `zigbee2mqtt` (adaptateur Zigbee identique), `portainer`.
- **Hôte reconstruit** : Terraform puis `./deploy.sh prod` (structure et configuration),
  arrêt de la pile (`docker compose stop`), restauration des dossiers de données comme
  ci-dessus, puis `docker compose start`.
- **Ensuite** : `sudo rm -rf /var/tmp/restauration` (contient des secrets : clé Zigbee,
  certificats, identifiants Node-RED).

Home Assistant (VM HAOS) n'est pas concerné : il utilise ses propres sauvegardes.

---

## NAS Synology — maintenance Restic

Playbook `playbooks/nas.yml` (groupe `[nas]`, sans `become`), déployé uniquement via le
workflow manuel **NAS — Restic maintenance**, jamais par `deploy.sh`. Mode dry-run
uniquement. Préparation du NAS, tâche DSM et contrôles :
[`services/synology/restic-maintenance/README.md`](../../services/synology/restic-maintenance/README.md).

---

## Flux de déploiement

```mermaid
flowchart TD
    A([./deploy.sh test|prod]) --> B[Authentification Bitwarden]
    B --> C[Récupération des secrets\nMQTT_PASSWORD\nZ2M_NETWORK_KEY\nCF_DNS_API_TOKEN]
    C --> D[ansible-playbook\ndocker-host.yml]

    D --> E[Rôle: common]
    D --> F[Rôle: docker]
    D --> G[Rôle: compose_deploy]

    E --> E1[Timezone]
    E --> E2[Packages de base]

    F --> F1[Dépôt APT Docker]
    F --> F2[Installation Docker]
    F --> F3[Service Docker]
    F --> F4[Groupe docker]

    G --> G1[Sync fichiers\ncompose via rsync]
    G1 --> G2[Render templates\n.env / traefik.yml\nZigbee2MQTT config]
    G2 --> G3[acme.json 600]
    G3 --> G4[Permissions\nMosquitto / Node-RED]
    G4 --> G5[Mot de passe\nMosquitto]
    G5 --> G6[docker compose pull]
    G6 --> G7[docker compose up -d]
    G7 --> G8[Reload Mosquitto]
```

---

## Variables clés

| Variable             | Défaut / Source                      | Description                              |
|----------------------|--------------------------------------|------------------------------------------|
| `timezone`           | `Europe/Paris`                       | Timezone de la machine                   |
| `acme_email`         | `remigrz@gmail.com`                  | Email pour les certificats Let's Encrypt |
| `docker_users`       | `[remi]`                             | Utilisateurs ajoutés au groupe docker    |
| `docker_data_root`   | `/srv/docker`                        | Répertoire racine des données Docker     |
| `compose_projects`   | liste de projets                     | Projets Docker Compose à déployer        |
| `domain_base`        | dépend de l'env                      | Domaine de base pour Traefik             |
| `cf_api_token`       | `lookup('env', 'CF_DNS_API_TOKEN')`  | Token API Cloudflare (depuis Bitwarden)  |
| `mqtt_user`          | `mqtt`                               | Utilisateur MQTT                         |
| `mqtt_password`      | `lookup('env', 'MQTT_PASSWORD')`     | Mot de passe MQTT (depuis Bitwarden)     |
| `smlight_serial_url` | dépend de l'env                      | URL série du coordinateur Zigbee         |
| `z2m_network_key`    | `lookup('env', 'Z2M_NETWORK_KEY')`   | Clé réseau Zigbee2MQTT (depuis Bitwarden)|
