# Sauvegardes

Vue d'ensemble. Les procédures détaillées restent dans les guides liés.

| Données | Mécanisme | Destination | Statut |
|---|---|---|---|
| Données Docker de `docker-prod-01` (`/srv/docker/core`) | Restic, rôle `restic_backup`, quotidien à 02:30 | REST Server du NAS, repository `docker-prod` (append-only) | Git ; un snapshot par jour constaté du 31/10 au 17/11/2026 ([compléments du 17/11/2026](#compléments-du-17112026)) |
| Données Docker de `docker-test-01` | Restic, timer installé à la main (03:00) | REST Server du NAS, repository `docker-test` | **hors dépôt** ; continuité et restauration constatées le 15/11/2026 ([audit](#audit-du-15112026)) |
| Repositories Restic | Contrôle quotidien, `forget` et `prune` planifiés sur le NAS | NAS (exécution locale) | Git (rôle `restic_maintenance`) ; premiers `forget` et `prune` automatiques constatés du 06 au 14/11/2026 ([audit](#audit-du-15112026)) ; `check --read-data` réussis les 01, 08 et 15/11/2026 ([compléments](#compléments-du-17112026)) |
| État Terraform | Copie horodatée par `tf.sh` après `apply` ou `destroy` | NAS (SSH, `rsync`) | Git |
| Home Assistant (`haos-prod-01`) | Sauvegardes natives de Home Assistant, quotidiennes, rétention 14, sauvegarde avant mise à jour | *Ce système* (disque de la VM), partage SMB `haos-backup-prod` du NAS, Home Assistant Cloud (Nabu Casa) | Hors IaC ; configuration constatée le 17/11/2026, première exécution automatique non vérifiée (voir [ci-dessous](#home-assistant)) |
| VM `haos-prod-01` (vzdump) | Tâche vzdump hebdomadaire, deux archives | Stockage Proxmox `Reminas` (NFS du NAS) | **Cible**, à vérifier ; aucune tâche ni archive au constat du 14/11/2026 (voir [ci-dessous](#vm-proxmox-vzdump)) |
| Autres VM Proxmox (vzdump) | — | — | Aucune sauvegarde décrite dans le dépôt ; aucune visible au constat du 14/11/2026 |
| NAS | Snapshots Btrfs (05:00) et Hyper Backup (06:00) | Destination Hyper Backup non documentée | Hors dépôt (DSM) ; Hyper Backup du dossier `Restic` constaté les 06 et 07/11/2026 ([audit](#audit-du-15112026)) ; snapshots Btrfs quotidiens constatés au 17/11/2026, rétention à long terme et restauration non vérifiées ([compléments](#compléments-du-17112026)) |
| Poste de travail `dev-agent-01` | — | — | Non documenté ; aucune sauvegarde vzdump au constat du 14/11/2026 |

## Restic

- **Sauvegarde des hôtes Docker** (client, append-only, cohérence à chaud, restauration) :
  [`infra/ansible/HOWTO.md`, « Sauvegarde Restic des hôtes Docker »](../infra/ansible/HOWTO.md#sauvegarde-restic-des-hôtes-docker).
- **Maintenance sur le NAS** (contrôle dry-run, rétention, `forget`, `prune`, tâches DSM,
  état réel au 31/10/2026) :
  [`services/synology/restic-maintenance/README.md`](../services/synology/restic-maintenance/README.md).

La sauvegarde de `docker-test-01` n'est pas activée par Ansible
(`restic_backup_enabled` absent de l'inventaire test, donc `false`) : son timer a été créé
manuellement pendant la validation. L'aligner sur l'IaC est un point ouvert.

### Couverture de Mosquitto, Zigbee2MQTT et Node-RED en prod

**Git (audit du 14/11/2026)** : les trois services sont couverts par la sauvegarde
quotidienne de `docker-prod-01`.

| Service | Données (bind mounts de [`docker-compose.yml`](../services/docker/core/docker-compose.yml)) | Couvert par `restic_backup_paths` (`/srv/docker/core`) |
|---|---|---|
| Mosquitto | `mosquitto/config`, `mosquitto/data` | oui ; `mosquitto/log` exclu volontairement |
| Zigbee2MQTT | `zigbee2mqtt/data` (dont `database.db`, `coordinator_backup.json`, `configuration.yaml`) | oui |
| Node-RED | `nodered/data` (dont `flows.json`, `flows_cred.json`) | oui |

- La pile est déployée dans `/srv/docker/core` (`group_vars/docker_hosts.yml` de prod) et
  n'utilise aucun volume Docker nommé : toutes les données sont sous le chemin sauvegardé.
- Les trois services sont copiés à chaud ; seul Portainer est arrêté. Limites connues
  (messages Mosquitto non encore enregistrés, `state.json` de Zigbee2MQTT non atomique) :
  tableau « Cohérence à chaud » du
  [`HOWTO`](../infra/ansible/HOWTO.md#sauvegarde-restic-des-hôtes-docker).
- La clé réseau Zigbee (`configuration.yaml`) et la clé de chiffrement des identifiants
  Node-RED (`.config.runtime.json`) sont dans la sauvegarde, chiffrée par Restic : le
  mot de passe du repository est donc indispensable à la reprise de la domotique.

**État réel** : voir l'[audit du 15/11/2026](#audit-du-15112026) et ses
[compléments du 17/11/2026](#compléments-du-17112026) : un snapshot `docker-prod` par jour
du 31/10 au 17/11/2026, premiers `forget` et `prune` automatiques réussis. Dernier test de
restauration de `docker-prod` documenté : 28/10/2026. Le 18/11/2026, la section `devices:`
de `zigbee2mqtt/data/configuration.yaml` a été extraite du snapshot `4a5cd195` pour une
réparation réelle (38 noms rétablis) : extraction d'un seul fichier, pas un test de
restauration complet ; voir
[l'incident Zigbee2MQTT](services-docker.md#incident-du-18112026--noms-zigbee2mqtt-effacés).

## Audit du 15/11/2026

**Source** : relevés faits par le propriétaire du 06 au 15/11/2026 (e-mails et journaux
DSM, commandes sur `docker-test-01`), rapportés ici sans avoir été rejoués. Ce sont des
constats datés : ils ne garantissent ni les exécutions suivantes ni l'état actuel.
Configuration de référence : rôles `restic_backup` et `restic_maintenance`, inventaires
`test` et `prod` (Git, `main`).

### Premières opérations automatiques `forget` et `prune` (constat, e-mails DSM)

| Date | Repository | Tâche DSM | Résultat |
|---|---|---|---|
| 06/11/2026 | `docker-test` | `forget` (ID 27) | `forget_done` : 6 snapshots supprimés, 37 conservés |
| 06/11/2026 | `docker-test` | `prune` (ID 28) | `prune_done` : 37 snapshots inchangés |
| 07/11/2026 | `docker-prod` | `forget` (ID 24) | `nothing_to_remove` : 11 snapshots conservés |
| 07/11/2026 | `docker-prod` | `prune` (ID 25) | `prune_done` : 11 snapshots inchangés |
| 13/11/2026 | `docker-test` | `forget` (ID 27) | `forget_done` : 6 snapshots supprimés, 38 conservés |
| 14/11/2026 | `docker-prod` | `forget` (ID 24) | `nothing_to_remove` : 18 snapshots conservés |

- Les six exécutions se terminent en code 0, et le contrôle préalable intégré à chacune de
  ces six opérations n'a signalé aucune erreur. Les premières opérations réelles sont donc
  **validées pour ces dates**, sans préjuger des suivantes (prochain `prune` : 04/12 en
  test, 05/12 en prod). Cela ne valide pas les contrôles quotidiens du NAS (voir
  « Restant à vérifier »).
- Analyse des résultats observés (calcul, non extrait des e-mails) : en prod, aucun
  snapshot n'avait plus de 30 jours (`keep-within 30d`) ; en test, les suppressions sont
  restées sous les seuils configurés de 10 snapshots et de 20 % (6 sur 43, puis 6 sur 44,
  soit environ 14 %).
- Les effectifs prod (4 au 31/10, 11 au 07/11, 18 au 14/11) correspondent à **une
  sauvegarde par jour**, sans le prouver jour par jour : la continuité prod n'était pas
  vérifiée directement au 15/11 (elle l'a été le 17/11/2026, voir
  [compléments](#compléments-du-17112026)).
- Ces e-mails ont été reçus pour des exécutions réussies : l'envoi fonctionne pour les
  tâches 24, 25, 27 et 28.

### Hyper Backup après `prune` (constat, journaux DSM)

| Date | `prune` terminé | Hyper Backup `Restic` |
|---|---|---|
| 06/11/2026 | `docker-test`, vers 04:15 | réussi, 06:00:03 à 06:00:31 |
| 07/11/2026 | `docker-prod`, vers 04:16 | réussi, 06:00:02 à 06:00:31 |

Fichier contrôlé : index de `docker-test` réécrit par `prune` le 06/11/2026 à 04:15:34,
`cce79f763f801fed10a152d41a0a9c112ed0633bd6c6d44d1241386e68a36f44` (167 730 octets,
`root:root 0400`, `synoacltool` : « It's Linux mode », toujours présent dans le
repository). Téléchargé depuis la version Hyper Backup du 06/11 vers 06:00 : SHA-256
identique à celui de l'original calculé sur le NAS ; c'est cette comparaison qui constitue
la vérification. Que le nom du fichier soit égal à son empreinte est attendu : le format
restic nomme ses fichiers d'après l'empreinte de leur contenu.

**Limites** : il s'agit d'un téléchargement vers un poste macOS, pas d'une restauration
sur le NAS ; la restitution des droits Unix et des ACL n'est pas démontrée, ni la
restauration complète d'un repository depuis Hyper Backup.

### Sauvegarde et restauration de `docker-test-01` (constat du 15/11/2026)

- **Continuité** : `restic-backup.timer` actif ; dernière exécution le 15/11/2026 à
  03:04:16 CET (`Result=success`, `ExecMainStatus=0`). Repository `952285ee` :
  40 snapshots, au moins un snapshot `core,test` chaque jour du 31/10 au 15/11/2026,
  dernier `180fa597` (15/11/2026, 03:04:14 CET).
- **Restauration** de `180fa597` avec `restic restore … --verify` dans un dossier isolé
  sous `/var/tmp`, services Docker non arrêtés : 3 245 fichiers et dossiers, 21,136 MiB,
  2 533 fichiers vérifiés, aucune erreur ; dossiers de Mosquitto, Zigbee2MQTT, Node-RED,
  Portainer et Traefik présents.
- **Comparaison** avec `/srv/docker/core` (`diff -rq --exclude=log`) : trois fichiers
  différents, `mosquitto/data/mosquitto.db` et `portainer/data/portainer.db` (bases
  vivantes), et `mosquitto/config/passwords`, modifié après le snapshot (15/11/2026,
  21:35). La cause de cette modification n'est pas établie (une régénération lors d'un
  déploiement est plausible, non démontrée).
- **Nettoyage** : dossier de restauration supprimé, absence confirmée ; rien d'autre
  supprimé.
- **Limite** : les données restaurées sont validées, pas le redémarrage des services à
  partir de ces données. Ce test passe par le client de test, installé hors dépôt ; la
  procédure versionnée (`restic-core restore-to`) concerne `docker-prod-01`.

### Compléments du 17/11/2026

**Source** : vérifications faites par le propriétaire le 17/11/2026, en lecture seule
(rapports JSON du NAS, interface DSM, commande sur `docker-prod-01`), rapportées ici sans
avoir été rejouées. Comme ci-dessus, ce sont des constats datés sur la période indiquée,
pas une garantie sur l'état actuel ni sur les exécutions suivantes.

#### Contrôles hebdomadaires `check --read-data` (constat, rapports JSON)

**Configuration Git** : `restic_maintenance_read_data` (défauts du rôle :
`weekdays: [7]`, `parts: 1`), donc lecture intégrale des données le dimanche, dans les
tâches de contrôle quotidiennes (ID 21 en prod, ID 26 en test ; voir
[« Vérification des données »](../services/synology/restic-maintenance/README.md#vérification-des-données)).

| Dimanche | `docker-test` (ID 26) | `docker-prod` (ID 21) |
|---|---|---|
| 01/11/2026 | ok | ok |
| 08/11/2026 | ok | ok |
| 15/11/2026 | ok | ok |

Chacun des six rapports indique `operation=control`, `status=ok`,
`read_data=--read-data`, `check.ok=True` et `findings=[]` : six contrôles complets réussis
aux dates observées. Le rapport de test du 01/11 est aussi le premier contrôle planifié de
la tâche 26.

**Limites** : ces contrôles prouvent que les packs référencés par l'index étaient
lisibles et intègres sur le disque du NAS à ces dates, pas qu'une restauration complète
fonctionne. Seuls les rapports du dimanche ont été consultés : les contrôles des autres
jours ne sont pas couverts.

#### Snapshots Btrfs du dossier `Restic` (constat, DSM Snapshot Replication)

**Configuration** : hors dépôt (DSM) ; le dépôt ne fait que supposer l'horaire de 05:00
pour placer la plage protégée des opérations réelles (`apply.protected`).

- Instantané local quotidien, vers 05:00.
- Politique de rétention affichée : 14 quotidiens, 8 hebdomadaires, 3 mensuels et les
  5 derniers instantanés.
- 17 points de restauration présents au 17/11/2026 ; des instantanés quotidiens sont
  visibles sur les autres dates de novembre.
- Instantanés postérieurs aux `prune` (voir [tableau Hyper Backup](#hyper-backup-après-prune-constat-journaux-dsm)) :
  06/11/2026 à 05:00:02 (après le `prune` de test, terminé vers 04:15) et 07/11/2026 à
  05:00:01 (après celui de prod, vers 04:16).

**Limites** : la planification et la présence de ces instantanés sont confirmées ;
l'application de la rétention à long terme (hebdomadaire, mensuelle) et la restauration
depuis un instantané Btrfs ne sont pas validées.

#### Continuité de `docker-prod` (constat, `docker-prod-01`)

**Configuration Git** : timer quotidien du rôle `restic_backup`, tags `core` et `prod`
(`inventories/prod/group_vars/docker_hosts.yml`), commande via le wrapper versionné
`/usr/local/sbin/restic-core`.

Lecture de `restic snapshots --json --no-lock --no-cache` par ce wrapper, filtrée sur les
tags `core` et `prod` : du 31/10 au 17/11/2026 inclus (18 jours), exactement un snapshot
par jour, aucun jour manquant, aucun doublon. La continuité quotidienne des snapshots de
production est confirmée sur cette période.

**Limite** : cela prouve l'existence des snapshots, pas leur restaurabilité (seul test
de restauration de `docker-prod` documenté : 28/10/2026, voir [`CLAUDE.md`](../CLAUDE.md)).

### Restant à vérifier (au 17/11/2026)

- Exécution des contrôles quotidiens du NAS (tâches 21 et 26) en dehors des dimanches
  01, 08 et 15/11/2026, et réglage des notifications de ces tâches dans DSM.
- Application de la rétention à long terme des snapshots Btrfs, et restauration depuis
  l'un d'eux.
- Test de restauration récent de `docker-prod` (aucun documenté depuis le 28/10/2026).
- Redémarrage fonctionnel des services Docker à partir d'un snapshot restauré.
- Restauration complète d'un repository Restic depuis Hyper Backup, sur le NAS.
- Home Assistant et Proxmox : hors du périmètre de cet audit (voir ci-dessous).

## État Terraform

[`tf.sh`](../infra/terraform/tf.sh) copie `terraform.tfstate` (et `terraform.tfstate.backup`
s'il existe) sur le NAS après chaque `apply` ou `destroy`, dans un dossier par
environnement et machine (`prod_pve_docker-host`…), avec un horodatage. L'état de
référence reste local au poste qui exécute Terraform : il faut le récupérer sur le NAS
avant de travailler depuis un autre poste. Aucune rétention n'est appliquée aux copies.

## Home Assistant

Non couvert par Restic. Deux niveaux, complémentaires : la sauvegarde Home Assistant est
configurée (constat du 17/11/2026, ci-dessous) ; le vzdump reste une **cible**.

| Niveau | Contenu | Destination | Usage |
|---|---|---|---|
| Sauvegarde Home Assistant | configuration, base de l'historique, modules complémentaires | *Ce système* + partage Synology `haos-backup-prod` + Home Assistant Cloud | reprise sur une HAOS neuve, retour arrière d'une mise à jour |
| vzdump de la VM | disque complet de `haos-prod-01` | stockage Proxmox `Reminas` | reprise rapide d'une VM identique, sur `pve` ou la tour de spare |

La copie *Ce système* est sur le disque de la VM et disparaît avec elle ; Synology et
vzdump finissent sur le même NAS. La copie Home Assistant Cloud est la seule hors site tant
que la destination de Hyper Backup n'est pas documentée.

### Constat du 17/11/2026 (`haos-prod-01`)

**Source** : vérification faite par le propriétaire le 17/11/2026 dans l'interface de Home
Assistant et sur le NAS, rapportée ici sans avoir été rejouée. Configuration hors IaC.

- Sauvegardes automatiques natives **quotidiennes**, vers trois emplacements : *Ce
  système*, Synology (partage SMB `haos-backup-prod`, monté par l'adresse IP LAN du
  Synology) et Home Assistant Cloud.
- Rétention portée à **14** sauvegardes.
- Sauvegarde automatique **avant mise à jour** activée.
- Une sauvegarde manuelle de 40,5 Mo a été vue sur le Synology.

**Non vérifié** : première exécution automatique sur chacun des trois emplacements ;
stockage de la clé de chiffrement dans Bitwarden ; restauration d'une de ces sauvegardes
(aucun test).

### Configuration des sauvegardes Home Assistant (procédure)

Configuration faite dans l'interface de Home Assistant (hors IaC, non versionnée). Elle ne
concerne que la prod ; aucune sauvegarde de `haos-test-01` n'est prévue.

**Côté NAS** (DSM) :

1. Dossier partagé dédié (`haos-backup-prod` au constat du 17/11/2026), sans accès invité.
2. Utilisateur DSM dédié (par exemple `ha-backup`), droits en lecture/écriture sur ce
   seul dossier, aucun autre service que SMB. Mot de passe généré et stocké dans
   Bitwarden ; il n'est saisi que dans Home Assistant.
3. SMB : version minimale SMB2 (SMB3 de préférence).

**Côté Home Assistant** (`haos-prod-01`, *Paramètres → Système*) :

1. *Stockage → Ajouter un stockage réseau* : nom `synology`, usage **Sauvegarde**,
   protocole SMB (ou NFS), serveur = adresse IP LAN du Synology, et non `nas.reminat.com`,
   qui pointe déjà vers Traefik (route `nas`, constat DNS du 17/11/2026) ; partage
   `haos-backup-prod` et identifiants ci-dessus.
2. *Sauvegardes → Paramètres* :
   - sauvegardes automatiques **quotidiennes**, contenu : paramètres et historique,
     dossiers, modules complémentaires ;
   - emplacements : *Ce système*, `synology` et **Home Assistant Cloud** (abonnement
     Nabu Casa actif) ; *Ce système* consomme le disque de la VM ;
   - rétention : 14 sauvegardes (Home Assistant Cloud n'en garde qu'une, la plus récente,
     avec une taille maximale imposée par Nabu Casa) ;
   - sauvegarde avant mise à jour activée.
3. **Clé de chiffrement** : Home Assistant chiffre les sauvegardes automatiques. Télécharger
   le kit d'urgence et stocker la clé dans Bitwarden ; sans elle, aucune restauration
   n'est possible, ni depuis Synology ni depuis Nabu Casa. Ne jamais la recopier ici.

**Contrôles attendus après configuration** (à dater dans ce fichier) : une sauvegarde
manuelle apparaît dans chaque emplacement (vue sur le Synology le 17/11/2026) ; la
première sauvegarde automatique aussi ; le fichier `.tar` est présent dans le dossier du
NAS ; la clé du kit d'urgence ouvre la sauvegarde (restauration de test, voir
[ci-dessous](#restauration-sur-la-tour-de-spare)).

## VM Proxmox (vzdump)

### Constat du 14/11/2026 (MCP Proxmox, lecture seule)

- Aucune tâche de sauvegarde planifiée ; aucune archive vzdump sur `local` ni `Reminas`.
  Proxmox signale les onze VM et templates comme non sauvegardés (inchangé depuis le
  03/11/2026).
- `haos-prod-01` (VMID 104) : disque système de 64 Gio et disque EFI sur `local-lvm`
  (thin), `backup=1` sur le disque, agent QEMU activé, démarrage automatique.
- Stockages acceptant le contenu `backup` :

  | Stockage | Type | Capacité | Libre | Remarque |
  |---|---|---|---|---|
  | `local` | répertoire, disque système de `pve` | 94 Gio | 80 Gio (9 % utilisé) | perdu avec l'hôte : inutile pour une reprise sur la tour de spare |
  | `Reminas` | NFS du NAS, partagé | 15,7 Tio | 4,5 Tio (71 % utilisé) | accessible depuis un autre Proxmox |

- **Git** : aucune tâche vzdump ni Proxmox Backup Server n'est déclarée.
- **Portée** : ce relevé ne voit ni Restic ni les sauvegardes internes de Home Assistant.

### Sauvegarde hebdomadaire de `haos-prod-01` (cible)

| Réglage | Valeur | Raison |
|---|---|---|
| VM | 104 (`haos-prod-01`) seulement | les autres VM se recréent par Terraform + Ansible + Restic |
| Stockage | `Reminas` | lisible depuis la tour de spare ; `local` disparaît avec `pve` |
| Planification | dimanche 01:00 | avant Restic (02:30), la maintenance du NAS (03:45 et samedi 04:00/04:15), les snapshots Btrfs (05:00) et Hyper Backup (06:00) |
| Mode | `snapshot` | pas d'interruption ; l'agent QEMU gèle les systèmes de fichiers |
| Compression | `zstd` | |
| Rétention | `keep-last=2` | deux archives : la plus récente et la précédente |
| Notification | en cas d'échec (*Datacenter → Notifications*) | à vérifier : la messagerie de `pve` n'est pas documentée |

Volume : au plus 64 Gio par archive avant compression (seuls les blocs alloués du disque
thin sont lus), soit moins de 3 % de l'espace libre de `Reminas` pour deux archives.

Création, hors IaC (la tâche vit dans `/etc/pve/jobs.cfg` de `pve`) : *Datacenter →
Sauvegarde → Ajouter*, avec les réglages ci-dessus et *Rétention → Garder les dernières :
2*. Équivalent en ligne de commande, sur `pve` :

```sh
pvesh create /cluster/backup --schedule 'sun 01:00' --vmid 104 --storage Reminas \
  --mode snapshot --compress zstd --prune-backups keep-last=2 --enabled 1
```

Options discutables :

- **Mode `stop`** au lieu de `snapshot` : copie cohérente même pour la base SQLite de
  l'historique, au prix de quelques minutes d'arrêt de Home Assistant chaque dimanche.
  Écarté : la sauvegarde Home Assistant quotidienne couvre déjà la cohérence applicative.
- **`keep-weekly=2`** au lieu de `keep-last=2` : identique tant qu'il n'y a qu'une
  exécution par semaine ; `keep-last` garde aussi les sauvegardes manuelles dans le
  compte, ce qui peut évincer une archive hebdomadaire. Une sauvegarde manuelle avant
  mise à jour majeure est donc à faire avec la rétention désactivée, ou sur `local`.
- **Déclarer la tâche en IaC** : non retenu pour une seule tâche ; à reconsidérer si
  d'autres VM s'ajoutent.

Contrôles après création : la tâche apparaît dans *Datacenter → Sauvegarde* ; une
exécution manuelle (*Exécuter maintenant*) se termine en `OK` dans le journal des tâches ;
l'archive `vzdump-qemu-104-*.vma.zst` apparaît dans `Reminas` ; après la troisième
exécution, il n'en reste que deux.

## Restauration sur la tour de spare

**Cible**, aucune étape n'a été exécutée. Le matériel de la tour de spare (processeur,
disques, réseau) et son installation Proxmox ne sont pas documentés dans le dépôt.

### Précautions

- Une copie restaurée de Home Assistant se connecte au broker MQTT de prod et peut piloter
  les appareils. **Pour un test**, démarrer la VM avec la carte réseau déconnectée
  (`link_down=1`) ou sur un pont isolé, et ne jamais la laisser tourner en même temps que
  `haos-prod-01` sur le réseau de prod.
- L'adresse MAC est conservée par `qmrestore` : deux VM identiques sur le même réseau se
  disputent l'adresse IP réservée. `--unique 1` génère une nouvelle MAC (test), à ne pas
  utiliser pour une vraie reprise.
- Aucun périphérique USB n'est attaché à `haos-prod-01` (le coordinateur Zigbee est sur
  l'hôte Docker) : la tour de spare n'a besoin d'aucun dongle pour HAOS.
- Ajouter `Reminas` sur la tour de spare **en lecture seule** pendant un test (option NFS
  `ro`), pour que sa rétention ou une erreur ne touche pas les archives de prod.

### A. Depuis l'archive vzdump (reprise rapide)

1. Proxmox VE installé sur la tour de spare, à une version au moins égale à celle de `pve`.
2. *Datacenter → Stockage → Ajouter → NFS* : même export que `Reminas`, contenu `backup`
   (lecture seule pour un test).
3. **Test** : restaurer sous un VMID libre, `VMID_TEST`, jamais 104 (réservé à la reprise
   réelle), puis isoler la VM **avant son premier démarrage**. Renseigner d'abord les deux
   variables ; les valeurs ci-dessous sont des **exemples à remplacer** :

   ```sh
   VMID_TEST=9104   # EXEMPLE : VMID libre sur la tour de spare, différent de 104
   ARCHIVE=/mnt/pve/Reminas/dump/vzdump-qemu-104-2026_11_22-01_00_02.vma.zst   # EXEMPLE : chemin complet
   ```

   Restauration et isolation. Le sous-shell s'arrête à la première erreur : un VMID
   invalide, égal à 104 ou déjà occupé, une archive absente ou un échec de `qmrestore`
   empêchent `qm set`.

   ```sh
   (
     set -eu
     case "$VMID_TEST" in
       ''|0*|*[!0-9]*) echo "VMID_TEST invalide : '$VMID_TEST'" >&2; exit 1 ;;
     esac
     if [ "$VMID_TEST" -lt 100 ] || [ "$VMID_TEST" -eq 104 ]; then
       echo "VMID_TEST refusé : $VMID_TEST (< 100, ou 104 réservé à la reprise réelle)" >&2; exit 1
     fi
     # Erreur si le VMID est déjà occupé
     pvesh get /cluster/nextid --vmid "$VMID_TEST" >/dev/null
     [ -f "$ARCHIVE" ] || { echo "Archive introuvable : $ARCHIVE" >&2; exit 1; }
     # Nouvelle MAC ; qmrestore ne démarre pas la VM
     qmrestore "$ARCHIVE" "$VMID_TEST" --storage local-lvm --unique 1
     # Carte déconnectée, pas de démarrage automatique
     qm set "$VMID_TEST" --net0 virtio,bridge=vmbr0,link_down=1 --onboot 0
   )
   ```

   Contrôle : chaque ligne `netN` affichée doit contenir `link_down=1`. Une interface
   supplémentaire restaurée sans cette option se déconnecte de la même façon
   (`qm set "$VMID_TEST" --netN …,link_down=1`).

   ```sh
   qm config "$VMID_TEST" | grep -E '^net[0-9]+:'
   ```

4. **Test**, seulement après ce contrôle : démarrer et ouvrir la console sous `VMID_TEST`,
   puis vérifier que HAOS démarre et que le cœur de Home Assistant répond
   (`ha core info`). Le bloc refuse de démarrer si `VMID_TEST` vaut 104 ou si une
   interface `netN` n'a pas `link_down=1`.

   ```sh
   (
     set -eu
     [ "$VMID_TEST" != 104 ] || { echo "VMID 104 réservé à la reprise réelle" >&2; exit 1; }
     conf=$(qm config "$VMID_TEST")
     if printf '%s\n' "$conf" | grep -E '^net[0-9]+:' | grep -vq 'link_down=1'; then
       echo "Interface netN connectée : démarrage refusé" >&2; exit 1
     fi
     qm start "$VMID_TEST"
     qm terminal "$VMID_TEST"   # ou noVNC, sur la VM VMID_TEST
   )
   ```

   Le lien réseau reste coupé pendant tout le test ; la VM est supprimée à la fin (voir
   [Contrôles de réussite d'un test](#contrôles-de-réussite-dun-test)). Ces commandes
   n'ont pas encore été exécutées sur Proxmox : à valider lors du premier test.
5. **Reprise réelle** seulement, sous le VMID 104 : arrêter définitivement `haos-prod-01`
   sur `pve` s'il existe encore, puis restaurer sans `--unique` (MAC conservée) ni
   déconnexion, démarrer et vérifier MQTT, l'accès via Traefik et les intégrations.
   `ARCHIVE` est renseignée comme à l'étape 3. Le sous-shell s'arrête à la première
   erreur : si le VMID 104 est déjà occupé sur l'hôte de secours, si l'archive est absente
   ou si `qmrestore` échoue, `qm start 104` n'est pas exécuté.

   ```sh
   (
     set -eu
     [ -f "$ARCHIVE" ] || { echo "Archive introuvable : $ARCHIVE" >&2; exit 1; }
     # Erreur si le VMID 104 est déjà occupé sur l'hôte de secours
     pvesh get /cluster/nextid --vmid 104 >/dev/null
     qmrestore "$ARCHIVE" 104 --storage local-lvm
     qm start 104
   )
   ```

### B. HAOS neuve et sauvegarde Home Assistant (indépendant de vzdump)

1. Créer une VM HAOS sur la tour de spare (template HAOS ou image officielle ; le module
   Terraform [`modules/haos`](../infra/terraform/modules/haos/) vise `pve` et n'a pas été
   essayé sur un autre nœud).
2. À l'accueil de Home Assistant, choisir **Restaurer depuis une sauvegarde** :
   - depuis Synology : récupérer le `.tar` dans le partage `haos-backup-prod` et le
     téléverser ;
   - depuis Nabu Casa : se connecter à Home Assistant Cloud pendant l'accueil et
     choisir la sauvegarde ;
   - saisir la clé de chiffrement (Bitwarden).
3. Mêmes vérifications et mêmes précautions réseau que ci-dessus.
4. Avant de relancer Ansible, vérifier le serveur HTTP et le proxy de confiance restaurés :
   [Home Assistant, « Sur un HAOS vierge ou restauré »](home-assistant.md#sur-un-haos-vierge-ou-restauré-reconstruction-tour-de-spare).
5. Ansible ([`haos.yml`](../infra/ansible/playbooks/haos.yml)) peut ensuite réappliquer
   `configuration.yaml` ; l'accès SSH 22222 doit être refait au bootstrap.

### Contrôles de réussite d'un test

- La VM démarre, Home Assistant affiche les intégrations, automatisations et tableaux de
  bord attendus, et l'historique jusqu'à la date de la sauvegarde.
- Durée de la restauration notée (objectif de reprise).
- La VM de test est supprimée après l'exercice ; le résultat est daté dans ce fichier.

## Reprise après sinistre (cible)

Scénario de référence repris de l'ancienne documentation, à valider par un exercice :

1. Réinstaller Proxmox VE et sa configuration réseau minimale (non documentée).
2. Rétablir l'accès au poste de travail, à GitHub et à Bitwarden.
3. Récupérer l'état Terraform sur le NAS, puis recréer les VM (`tf.sh`).
4. Bootstrap HAOS, puis `deploy.sh` pour configurer les hôtes.
5. Restaurer les données Docker depuis Restic et Home Assistant depuis vzdump ou ses
   sauvegardes (voir [Restauration sur la tour de spare](#restauration-sur-la-tour-de-spare)).

Les étapes 1 et 2 ne sont pas documentées ; la restauration de Home Assistant est décrite
mais non testée.

## Reste à faire et à tester

| Étape | Statut au 17/11/2026 |
|---|---|
| Constater les sauvegardes Restic de prod depuis le 31/10 (journal du service, `snapshots`, rapports du contrôle du NAS, `forget` et `prune` du 07/11) | fait : [audit du 15/11/2026](#audit-du-15112026) et [compléments du 17/11/2026](#compléments-du-17112026) ; points ouverts dans « Restant à vérifier » |
| Restaurer depuis Restic un dossier de Mosquitto, Zigbee2MQTT et Node-RED et le comparer | prod : dernier test le 28/10/2026 (restauration sur disque), extraction d'un fichier de Zigbee2MQTT le 18/11/2026 ([incident](services-docker.md#incident-du-18112026--noms-zigbee2mqtt-effacés)) ; test : restauration isolée le 15/11/2026 |
| Vérifier qu'un snapshot `docker-prod` postérieur au 18/11/2026 contient les 39 noms Zigbee2MQTT | à vérifier |
| Configurer le stockage Synology et la sauvegarde Home Assistant vers Synology et Nabu Casa | configuré, constat du 17/11/2026 ([ci-dessus](#constat-du-17112026-haos-prod-01)) |
| Constater la première sauvegarde automatique Home Assistant sur *Ce système*, Synology et Home Assistant Cloud | à vérifier |
| Stocker la clé de chiffrement Home Assistant dans Bitwarden | à faire ou à confirmer |
| Créer la tâche vzdump hebdomadaire de `haos-prod-01` | à vérifier ; absente au constat du 14/11/2026 |
| Vérifier la notification e-mail de `pve` | non vérifié |
| Test de restauration de `haos-prod-01` sur la tour de spare (voies A et B) | à vérifier : jamais exécuté ; matériel non documenté |
| Documenter la destination de Hyper Backup (copie hors site) | non documenté |
