# Sécurité

## Dépôt public

Le dépôt `reminat/remidom` est **public**. Ne jamais y publier :

- secret, mot de passe, jeton, clé privée, clé réseau Zigbee, valeur d'un secret Bitwarden ;
- adresse MAC, adresse IP par appareil, nom ou mot de passe de réseau Wi-Fi ;
- inventaire détaillé des clients réseau, règles de pare-feu détaillées ;
- description d'une faiblesse exploitable (chemin d'accès, scénario d'attaque).

Les noms de secrets, les adresses de sous-réseaux et les noms d'hôtes déjà présents dans la
configuration peuvent être cités.

## Gestion des secrets

Deux services Bitwarden (cloud) :

- **Password Manager** (`bw`) : contient l'élément `bws_machine_token`, dont le mot de passe
  est le jeton d'accès Secrets Manager utilisé par les scripts lancés depuis le poste.
- **Secrets Manager** (`bws`) : contient les secrets lus par les scripts au moment de
  l'exécution, par leur clé (*key*) ou leur UUID.

Les scripts résolvent la clé en UUID avec `bws secret list`, puis lisent la valeur. Les
valeurs ne sont jamais écrites dans le dépôt ; sur les hôtes, elles se retrouvent dans des
fichiers rendus par Ansible (par exemple `.env`, `/etc/restic/`), avec des droits
restreints.

### Catalogue des secrets (noms seulement)

Noms par défaut lus dans les scripts (Git). Une variable d'environnement permet parfois de
désigner un autre secret.

| Secret Secrets Manager | Utilisé par | Variable de remplacement |
|---|---|---|
| `proxmox_endpoint` | [`tf.sh`](../infra/terraform/tf.sh), [`bootstrap-haos.sh`](../infra/proxmox/scripts/bootstrap-haos.sh) | — |
| `proxmox_api_token_id` | `tf.sh` | — |
| `proxmox_api_token_secret` | `tf.sh` | — |
| `ssh_authorized_keys` (tableau JSON de clés publiques) | `tf.sh` (hôtes Docker), `bootstrap-haos.sh` | `SSH_KEYS_SECRET_REF` (bootstrap seulement) |
| `mqtt_password` | [`deploy.sh`](../infra/ansible/deploy.sh) | `MQTT_SECRET_REF` |
| `zigbee_network_key_<env>` (JSON) | `deploy.sh` | `Z2M_NETWORK_KEY_SECRET_REF` |
| `cloudflare_dns_api_token` | `deploy.sh` | `CF_DNS_API_TOKEN_SECRET_REF` |
| `restic_rest_password_<env>` (facultatif) | `deploy.sh` | `RESTIC_REST_PASSWORD_SECRET_REF` |
| `restic_password_<env>` (facultatif) | `deploy.sh` | `RESTIC_PASSWORD_SECRET_REF` |

| Autre secret | Emplacement | Utilisé par |
|---|---|---|
| `bws_machine_token` | Password Manager (mot de passe de l'élément) | `tf.sh`, `deploy.sh` (hors CI), `bootstrap-haos.sh` |
| `BWS_ACCESS_TOKEN` | Secret GitHub | workflow Deploy |
| `ANSIBLE_SSH_KEY` | Secret GitHub | workflow Deploy |
| `NAS_SSH_KEY` | Secret de l'environnement GitHub `test` | workflow NAS — Restic maintenance |
| Mot de passe des repositories Restic sur le NAS | Fichiers sur le NAS, hors dépôt | maintenance Restic ([README](../services/synology/restic-maintenance/README.md)) |

Le mot de passe d'un repository Restic doit aussi être conservé hors de l'hôte : sans lui,
aucune restauration n'est possible.

## Accès

- **VM Ubuntu** : utilisateur `remi`, clés SSH uniquement, root interdit en SSH
  (cloud-init, Git).
- **VM HAOS** : `root` par clé SSH sur le port 22222.
- **Proxmox** : jeton API pour Terraform, SSH root par clé dédiée pour les templates.
- **NAS** : compte de déploiement dédié, clé restreinte ; clé d'hôte vérifiée contre
  [`nas_known_hosts`](../infra/ansible/nas_known_hosts).
- **CI** : clé SSH dédiée aux runners (voir [CI/CD](ci-cd.md)).
- **UniFi (consultation)** : compte de service en lecture seule, utilisé uniquement par le
  MCP UniFi du poste (voir [Poste de travail](poste-de-travail.md#mcp-unifi)).

## Détection de secrets

| Contrôle | Portée | Définition |
|---|---|---|
| Hook pre-commit Gitleaks | commits locaux, si `pre-commit install` a été fait | [`.pre-commit-config.yaml`](../.pre-commit-config.yaml) |
| Workflow *Secrets Scan* | chaque PR, chaque push sur `main` | [`secrets-scan.yml`](../.github/workflows/secrets-scan.yml) |
| Gitleaks sur tout l'historique | avant chaque déploiement | [`gitleaks-full.yml`](../.github/workflows/gitleaks-full.yml) |

Configuration : [`.gitleaks.toml`](../.gitleaks.toml) (règles par défaut, exceptions pour
les fichiers de verrouillage Terraform et les clés SSH publiques du snippet cloud-init).

## Points ouverts

- **Segmentation réseau** : aucune règle de pare-feu personnalisée ; chantier futur (voir
  [Matériel et réseau](materiel-reseau.md#segmentation)).
- **Vérification TLS** : certains accès internes reposent sur des certificats auto-signés ;
  le durcissement est à traiter hors de cette documentation.
- **Fichier `.claude/settings.local.json`** : suivi par Git alors qu'il s'agit d'un réglage
  local de Claude Code ; son retrait du suivi est à décider.
