# Sauvegardes

Vue d'ensemble. Les procédures détaillées restent dans les guides liés.

| Données | Mécanisme | Destination | Statut |
|---|---|---|---|
| Données Docker de `docker-prod-01` (`/srv/docker/core`) | Restic, rôle `restic_backup`, quotidien à 02:30 | REST Server du NAS, repository `docker-prod` (append-only) | Git ; constat du 31/10/2026 dans le README Restic ; continuité quotidienne non vérifiée directement (voir [audit du 15/11/2026](#audit-du-15112026)) |
| Données Docker de `docker-test-01` | Restic, timer installé à la main (03:00) | REST Server du NAS, repository `docker-test` | **hors dépôt** ; continuité et restauration constatées le 15/11/2026 ([audit](#audit-du-15112026)) |
| Repositories Restic | Contrôle quotidien, `forget` et `prune` planifiés sur le NAS | NAS (exécution locale) | Git (rôle `restic_maintenance`) ; premiers `forget` et `prune` automatiques constatés du 06 au 14/11/2026 ([audit](#audit-du-15112026)) |
| État Terraform | Copie horodatée par `tf.sh` après `apply` ou `destroy` | NAS (SSH, `rsync`) | Git |
| Home Assistant | Sauvegardes propres à Home Assistant | Non documenté | Non vérifié |
| VM Proxmox (vzdump) | — | — | Aucune sauvegarde décrite dans le dépôt ; aucune visible au constat du 03/11/2026 (voir [ci-dessous](#vm-proxmox-vzdump)) |
| NAS | Snapshots Btrfs (05:00) et Hyper Backup (06:00) | Destination Hyper Backup non documentée | Hyper Backup du dossier `Restic` constaté les 06 et 07/11/2026 ([audit](#audit-du-15112026)) ; snapshots Btrfs non vérifiés |
| Poste de travail `dev-agent-01` | — | — | Non documenté ; aucune sauvegarde vzdump au constat du 03/11/2026 |

## Restic

- **Sauvegarde des hôtes Docker** (client, append-only, cohérence à chaud, restauration) :
  [`infra/ansible/HOWTO.md`, « Sauvegarde Restic des hôtes Docker »](../infra/ansible/HOWTO.md#sauvegarde-restic-des-hôtes-docker).
- **Maintenance sur le NAS** (contrôle dry-run, rétention, `forget`, `prune`, tâches DSM,
  état réel au 31/10/2026) :
  [`services/synology/restic-maintenance/README.md`](../services/synology/restic-maintenance/README.md).

La sauvegarde de `docker-test-01` n'est pas activée par Ansible
(`restic_backup_enabled` absent de l'inventaire test, donc `false`) : son timer a été créé
manuellement pendant la validation. L'aligner sur l'IaC est un point ouvert.

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
  sauvegarde par jour**, sans le prouver jour par jour : la continuité prod n'est pas
  vérifiée directement.
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

### Restant à vérifier

- Exécution des contrôles quotidiens du NAS (tâches 21 et 26) sur toute la période.
- Résultat des `check --read-data` des dimanches 01, 08 et 15/11/2026.
- Existence et rétention des snapshots Btrfs du dossier `Restic`.
- Continuité quotidienne de `docker-prod`, par ses journaux ou ses snapshots datés.
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

Non couvert par Restic. Deux scénarios de reprise figurent dans l'ancienne documentation
(Legacy, non testés) : restaurer une sauvegarde vzdump de la VM, ou recréer la VM avec
Terraform puis restaurer une sauvegarde Home Assistant. Aucun des deux n'est outillé dans
le dépôt.

## VM Proxmox (vzdump)

- **Git** : aucune tâche vzdump ni Proxmox Backup Server n'est déclarée.
- **Constat du 03/11/2026** (MCP Proxmox, lecture seule) : aucune tâche de sauvegarde
  planifiée, aucune sauvegarde présente sur les stockages `local` et `Reminas`, aucun
  snapshot ; Proxmox signale les neuf VM et templates comme non sauvegardés.
- **Portée** : ce constat ne concerne que les sauvegardes au niveau de Proxmox. Il ne dit
  rien des sauvegardes Restic des hôtes Docker ni des sauvegardes internes de Home
  Assistant, qui ne passent pas par Proxmox et ne sont pas visibles par ce relevé.
- **Conséquence** : `dev-agent-01` et les VM hors Terraform n'ont aucune sauvegarde au
  niveau de la VM ; leur restauration n'est pas possible depuis Proxmox. Les VM Terraform
  se recréent, mais leurs données dépendent de Restic (hôtes Docker) ou des sauvegardes
  Home Assistant.
- **Cible** : non définie (voir « Stratégie de sauvegarde de Home Assistant et des VM
  Proxmox » dans les [décisions à formaliser](decisions/index.md#à-formaliser-justification-non-établie)).

## Reprise après sinistre (cible)

Scénario de référence repris de l'ancienne documentation, à valider par un exercice :

1. Réinstaller Proxmox VE et sa configuration réseau minimale (non documentée).
2. Rétablir l'accès au poste de travail, à GitHub et à Bitwarden.
3. Récupérer l'état Terraform sur le NAS, puis recréer les VM (`tf.sh`).
4. Bootstrap HAOS, puis `deploy.sh` pour configurer les hôtes.
5. Restaurer les données Docker depuis Restic et Home Assistant depuis ses sauvegardes.

Les étapes 1, 2 et la restauration de Home Assistant ne sont pas documentées.
