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
| `code.<domaine>` | code-server sur `dev-agent-01` (constat du 02/11/2026), hors Docker | [`code.yml.j2`](../services/docker/core/traefik/dynamic/code.yml.j2) | prod |
| `nas.<domaine>` | DSM (NAS Synology) par son adresse LAN, middleware `lan-vpn-only`, certificat vérifié ([procédure](../infra/ansible/HOWTO.md#dsm-nas--certificat-vérifiable-par-traefik)) | [`nas.yml.j2`](../services/docker/core/traefik/dynamic/nas.yml.j2) | prod |

Les routes `proxmox`, `unifi`, `code` et `nas` ne sont rendues que si leur URL cible est
définie (`proxmox_url`, `unifi_url`, `code_url`, `nas_url`) : c'est le cas en prod seulement.

La publication de ces noms sur Internet (enregistrements DNS publics, redirection de ports
sur la passerelle) n'est pas décrite dans le dépôt : non vérifié.

## Données

Données persistantes dans `/srv/docker/core/<service>/` : base Mosquitto, base et
configuration Zigbee2MQTT (noms des appareils compris, écrits par Zigbee2MQTT et conservés
par Ansible), flux Node-RED, base Portainer, `traefik/acme/acme.json`.
Sauvegarde et cohérence à chaud service par service :
[`HOWTO.md`, « Sauvegarde Restic des hôtes Docker »](../infra/ansible/HOWTO.md#sauvegarde-restic-des-hôtes-docker).

### Incident du 18/11/2026 : noms Zigbee2MQTT effacés

Incident du 14 au 19/11/2026, identifié le 18/11/2026, clos le 19/11/2026. Constats réels
datés ; sources : journaux des runs `Deploy`, inspection en lecture seule de
`docker-prod-01`, snapshot Restic et constats du propriétaire du homelab. Heures en UTC.

**Bilan final (vérifié le 19/11/2026).**

| Point | Résultat |
|---|---|
| Correction | PR [#35](https://github.com/reminat/remidom/pull/35) fusionnée le 19/11/2026, commit `60bb5cb8206630e1f9b85f65e3591bad885e2838` |
| Test réel sur `docker-test-01` | renommage `TEST-PR35-Ampoule` conservé après déploiement (run [44612096319](https://github.com/reminat/remidom/actions/runs/44612096319)) |
| Déploiement prod | réussi (run [44614468804](https://github.com/reminat/remidom/actions/runs/44614468804)) |
| Contrôle après déploiement | 39 appareils configurés, 39 noms conservés ; SHA-256 de `configuration.yaml` identique avant et après ; date de démarrage de Zigbee2MQTT inchangée |
| Comparaison historique | 18 chemins YAML hors `devices:` présents à l'identique dans le snapshot du 14/11 et dans la configuration actuelle ; valeurs non comparées |
| Sauvegarde Restic | snapshot `c37a68be` du 19/11/2026, 01:30 : 39 appareils et 39 noms enregistrés |

**Symptômes et impact.** En prod, les noms personnalisés des appareils Zigbee avaient
disparu dans Zigbee2MQTT et dans Home Assistant (constat du propriétaire, 18/11/2026). Le
réseau Zigbee lui-même n'est pas en cause : le modèle reprenait la clé réseau de Bitwarden,
et `database.db` n'est pas géré par Ansible.

**Cause racine.** Le rôle `compose_deploy` rendait `zigbee2mqtt/data/configuration.yaml` avec
`ansible.builtin.template`, qui remplaçait tout le fichier. Le modèle n'a pas de section
`devices:` : les noms écrits par Zigbee2MQTT étaient effacés à chaque déploiement dont le
rendu différait du fichier en place.

**Chronologie.**

| Date | Fait |
|---|---|
| 14/11/2026 | Le snapshot Restic `4a5cd195` contient 38 noms personnalisés. |
| 14/11, ≈ 19:50 | Déploiement prod : le fichier est remplacé intégralement, la section `devices:` disparaît. |
| 15/11/2026 | Le snapshot Restic du jour ne contient plus aucun nom. Le déploiement prod de ce jour ne change rien (fichier déjà identique au modèle). |
| 17/11, ≈ 20:13 | Nouveau remplacement du fichier par Ansible. |
| 18/11/2026 | Problème identifié ; restauration sélective des 38 noms ; 39<sup>e</sup> appareil renommé à la main. |
| 19/11, 01:30 | Snapshot Restic `c37a68be` : 39 appareils et 39 noms enregistrés. |
| 19/11/2026 | PR [#35](https://github.com/reminat/remidom/pull/35) validée sur `docker-test-01` (run [44612096319](https://github.com/reminat/remidom/actions/runs/44612096319)). |
| 19/11/2026 | Inspection en lecture seule du fichier de prod (chemins de clés, sans valeurs). |
| 19/11/2026 | PR #35 fusionnée dans `main` (commit `60bb5cb8206630e1f9b85f65e3591bad885e2838`). |
| 19/11/2026 | Déploiement prod réussi (run [44614468804](https://github.com/reminat/remidom/actions/runs/44614468804)). |

**Restauration (18/11/2026).** Seule la section `devices:` a été reprise du snapshot Restic
`4a5cd195` (14/11/2026) : 38 noms restaurés sélectivement sur `docker-prod-01`, sans
toucher à la clé réseau ni à `database.db`. Le dernier appareil a été renommé à la main.
Les 39 noms sont revenus dans Zigbee2MQTT et Home Assistant (constat du propriétaire).

Sauvegarde après restauration : le snapshot Restic `c37a68be` (19/11/2026, 01:30) contient
39 appareils et 39 noms (vérifié le 19/11/2026). Ce constat ne garantit pas les snapshots
suivants.

**Correction permanente.** PR [#35](https://github.com/reminat/remidom/pull/35) : le module
`z2m_config` fusionne les seules clés gérées par Ansible dans le fichier existant. Noms,
groupes et autres réglages écrits par Zigbee2MQTT sont conservés. La clé réseau n'est jamais
réécrite sur un fichier existant ; le déploiement s'arrête si elle diffère de Bitwarden. Un
changement de clé gérée qui impose de redémarrer Zigbee2MQTT exige une autorisation
explicite (`z2m_restart_allowed`, désactivée par défaut). Détails :
[`HOWTO.md`](../infra/ansible/HOWTO.md#zigbee2mqtt--configurationyaml).

**Validation en test (19/11/2026, run
[44612096319](https://github.com/reminat/remidom/actions/runs/44612096319), branche
`fix/z2m-config-merge`, commit `3184909`).**

- Un appareil de `docker-test-01` a été renommé `TEST-PR35-Ampoule` avant le déploiement ; le
  nom subsiste après (constat du propriétaire).
- Journal Ansible : analyse `ok`, tâche « propriétaire et droits » `ok`, et les tâches d'arrêt
  du conteneur, d'écriture et de redémarrage `skipping`. Aucune réécriture du YAML ni aucun
  arrêt explicite de Zigbee2MQTT.

**Inspection de la prod (19/11/2026, lecture seule).** Les 18 chemins YAML hors `devices:`
sont présents à la fois dans le fichier actuel et dans le snapshot `4a5cd195`. Seuls les
chemins ont été comparés, pas les valeurs.

**Validation en prod (19/11/2026, run
[44614468804](https://github.com/reminat/remidom/actions/runs/44614468804), `main`, commit
`60bb5cb`).**

- Journal Ansible : mêmes résultats qu'en test (analyse et droits `ok` ; arrêt, écriture et
  redémarrage `skipping`).
- Constats après déploiement : 39 appareils configurés, 39 noms sur 39 conservés ; SHA-256
  de `configuration.yaml` identique avant et après ; conteneur Zigbee2MQTT toujours actif,
  date de démarrage inchangée.

**Risques résiduels.**

- Le contrôle des chemins YAML ne vérifie pas les valeurs. Deux cas n'auraient pas été
  détectés : une clé gérée par le modèle (par exemple `advanced.transmit_power`), modifiée
  auparavant dans l'interface et remise à la valeur du modèle le 14/11 ou le 17/11 ; une clé
  non gérée (par exemple `advanced.pan_id`, `advanced.ext_pan_id`) effacée puis réécrite par
  Zigbee2MQTT avec une autre valeur. Non vérifié.
- `docker compose pull` puis `docker compose up -d` s'exécutent à chaque déploiement,
  indépendamment de la fusion. Ils peuvent recréer le conteneur Zigbee2MQTT, par exemple
  après une nouvelle image ou une modification de `docker-compose.yml`. La tâche
  `Docker compose up` est toujours signalée `changed` : le journal ne permet pas de
  distinguer une recréation. Une recréation ne touche pas aux noms, qui sont dans le
  dossier monté `zigbee2mqtt/data`, mais interrompt le service.

## Mises à jour d'images

Les images sont épinglées par tag dans [`docker-compose.yml`](../services/docker/core/docker-compose.yml).
Une mise à jour passe par une PR, un déploiement en test, puis la prod.
