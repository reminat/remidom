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

Le runner tourne sur `docker-prod` et exécute les déploiements automatiquement :
- Push sur `feature/**` → deploy sur `test`
- Push sur `main` → deploy sur `prod`

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
