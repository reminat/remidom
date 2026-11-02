# Services Docker

Une seule pile, `core`, définie dans [`services/docker/core/`](../services/docker/core/) et
déployée par le rôle Ansible `compose_deploy` dans `/srv/docker/core` sur chaque hôte
Docker. Le même fichier Compose sert à `test` et `prod` ; seules les variables changent
(`domain_base`, coordinateur Zigbee, cibles des routes).

Procédure de déploiement et détail du rôle :
[`infra/ansible/HOWTO.md`](../infra/ansible/HOWTO.md#déploiement).

## Services (Git)

| Service | Image | Rôle | Accès |
|---|---|---|---|
| Traefik | `traefik:v3.6.11` | Reverse proxy HTTPS, certificats Let's Encrypt | ports 80 et 443 de l'hôte |
| Mosquitto | `eclipse-mosquitto:2.0.22` | Broker MQTT (authentification par mot de passe) | ports 1883 (MQTT) et 9001 (MQTT sur WebSocket) de l'hôte |
| Zigbee2MQTT | `koenkk/zigbee2mqtt:2.13.0` | Pont Zigbee ↔ MQTT, coordinateur SMLIGHT en TCP | `z2m.<domaine>` |
| Node-RED | `nodered/node-red:4.1.8` | Automatisations | `nodered.<domaine>` |
| Portainer | `portainer/portainer-ce:2.39.1` | Administration Docker | `portainer.<domaine>` |

Coordinateur Zigbee : adaptateur `ember` en prod, `ezsp` en test
(`group_vars/docker_hosts.yml`).

## Traefik

- Redirection HTTP → HTTPS ; certificats par challenge DNS Cloudflare (jeton dans
  Bitwarden, voir [Sécurité](securite.md)).
- Fournisseur Docker avec `exposedByDefault: false` : seuls les conteneurs étiquetés sont
  publiés.
- Fournisseur fichier (`traefik/dynamic/`) pour les services hors Docker.

### Routes (Git)

`<domaine>` vaut `reminat.com` en prod et `test.reminat.com` en test.

| Nom d'hôte | Cible | Définition | Environnements |
|---|---|---|---|
| `traefik.<domaine>` | Tableau de bord Traefik | label Docker | test, prod |
| `z2m.<domaine>` | Zigbee2MQTT | label Docker | test, prod |
| `nodered.<domaine>` | Node-RED | label Docker | test, prod |
| `portainer.<domaine>` | Portainer | label Docker | test, prod |
| `haos.<domaine>` | Home Assistant (`haos_url`) | [`haos.yml.j2`](../services/docker/core/traefik/dynamic/haos.yml.j2) | test, prod |
| `proxmox.<domaine>` | Interface Proxmox, middleware `lan-vpn-only` | [`proxmox.yml.j2`](../services/docker/core/traefik/dynamic/proxmox.yml.j2) | prod |
| `unifi.<domaine>` | Interface UniFi | [`unifi.yml.j2`](../services/docker/core/traefik/dynamic/unifi.yml.j2) | prod |
| `code.<domaine>` | Service `code` hors Docker | [`code.yml.j2`](../services/docker/core/traefik/dynamic/code.yml.j2) | prod |

Les routes `proxmox`, `unifi` et `code` ne sont rendues que si leur URL cible est définie
(`proxmox_url`, `unifi_url`, `code_url`) : c'est le cas en prod seulement.

La publication de ces noms sur Internet (enregistrements DNS publics, redirection de ports
sur la passerelle) n'est pas décrite dans le dépôt : non vérifié.

## Données

Données persistantes dans `/srv/docker/core/<service>/` : base Mosquitto, base et
configuration Zigbee2MQTT, flux Node-RED, base Portainer, `traefik/acme/acme.json`.
Sauvegarde et cohérence à chaud service par service :
[`HOWTO.md`, « Sauvegarde Restic des hôtes Docker »](../infra/ansible/HOWTO.md#sauvegarde-restic-des-hôtes-docker).

## Mises à jour d'images

Les images sont épinglées par tag dans [`docker-compose.yml`](../services/docker/core/docker-compose.yml).
Une mise à jour passe par une PR, un déploiement en test, puis la prod.
