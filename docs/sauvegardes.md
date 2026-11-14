# Sauvegardes

Vue d'ensemble. Les procédures détaillées restent dans les guides liés.

| Données | Mécanisme | Destination | Statut |
|---|---|---|---|
| Données Docker de `docker-prod-01` (`/srv/docker/core`) | Restic, rôle `restic_backup`, quotidien à 02:30 | REST Server du NAS, repository `docker-prod` (append-only) | Git ; constat du 31/10/2026 dans le README Restic |
| Données Docker de `docker-test-01` | Restic, timer installé à la main (03:00) | REST Server du NAS, repository `docker-test` | **hors dépôt** ; constat du 31/10/2026 dans le README Restic |
| Repositories Restic | Contrôle quotidien, `forget` et `prune` planifiés sur le NAS | NAS (exécution locale) | Git (rôle `restic_maintenance`) ; constat du 31/10/2026 |
| État Terraform | Copie horodatée par `tf.sh` après `apply` ou `destroy` | NAS (SSH, `rsync`) | Git |
| Home Assistant (`haos-prod-01`) | Sauvegardes propres à Home Assistant | Partage Synology et Home Assistant Cloud (Nabu Casa) | **Cible**, hors IaC ; configuration réelle non vérifiée (voir [ci-dessous](#home-assistant)) |
| VM `haos-prod-01` (vzdump) | Tâche vzdump hebdomadaire, deux archives | Stockage Proxmox `Reminas` (NFS du NAS) | **Cible** ; aucune tâche ni archive au constat du 14/11/2026 (voir [ci-dessous](#vm-proxmox-vzdump)) |
| Autres VM Proxmox (vzdump) | — | — | Aucune sauvegarde décrite dans le dépôt ; aucune visible au constat du 14/11/2026 |
| NAS | Snapshots Btrfs (05:00) et Hyper Backup (06:00) | Destination Hyper Backup non documentée | Mentionné dans le README Restic ; non détaillé |
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

**État réel** : dernier constat daté au 31/10/2026 (sauvegarde planifiée réussie,
`check` sans erreur, contrôle du NAS `ok` : voir le
[README Restic](../services/synology/restic-maintenance/README.md#état-de-la-production-au-31102026)).
**Non vérifié le 14/11/2026** : aucune clé d'hôte SSH connue sur `dev-agent-01` pour
`docker-prod-01` ni pour le NAS ; la connexion a été refusée plutôt que d'accepter une clé
non vérifiée. Les exécutions depuis le 31/10, les rapports du contrôle quotidien et les
premières exécutions planifiées de `forget` et `prune` (07/11) restent à constater.

## État Terraform

[`tf.sh`](../infra/terraform/tf.sh) copie `terraform.tfstate` (et `terraform.tfstate.backup`
s'il existe) sur le NAS après chaque `apply` ou `destroy`, dans un dossier par
environnement et machine (`prod_pve_docker-host`…), avec un horodatage. L'état de
référence reste local au poste qui exécute Terraform : il faut le récupérer sur le NAS
avant de travailler depuis un autre poste. Aucune rétention n'est appliquée aux copies.

## Home Assistant

Non couvert par Restic. **Cible** à deux niveaux, complémentaires :

| Niveau | Contenu | Destination | Usage |
|---|---|---|---|
| Sauvegarde Home Assistant | configuration, base de l'historique, modules complémentaires | partage Synology + Home Assistant Cloud | reprise sur une HAOS neuve, retour arrière d'une mise à jour |
| vzdump de la VM | disque complet de `haos-prod-01` | stockage Proxmox `Reminas` | reprise rapide d'une VM identique, sur `pve` ou la tour de spare |

Les deux finissent sur le même NAS (sauf la copie Nabu Casa) : la copie Home Assistant
Cloud est la seule hors site tant que la destination de Hyper Backup n'est pas documentée.

### Sauvegardes Home Assistant vers Synology et Nabu Casa (cible)

Configuration faite dans l'interface de Home Assistant (hors IaC, non versionnée). Elle ne
concerne que la prod ; aucune sauvegarde de `haos-test-01` n'est prévue.

**Côté NAS** (DSM) :

1. Dossier partagé dédié (par exemple `ha-backup`), sans accès invité.
2. Utilisateur DSM dédié (par exemple `ha-backup`), droits en lecture/écriture sur ce
   seul dossier, aucun autre service que SMB. Mot de passe généré et stocké dans
   Bitwarden ; il n'est saisi que dans Home Assistant.
3. SMB : version minimale SMB2 (SMB3 de préférence).

**Côté Home Assistant** (`haos-prod-01`, *Paramètres → Système*) :

1. *Stockage → Ajouter un stockage réseau* : nom `synology`, usage **Sauvegarde**,
   protocole SMB (ou NFS), serveur = nom DNS du NAS, partage et identifiants ci-dessus.
2. *Sauvegardes → Paramètres* :
   - sauvegardes automatiques **quotidiennes**, contenu : paramètres et historique,
     dossiers, modules complémentaires ;
   - emplacements : `synology` et **Home Assistant Cloud** (abonnement Nabu Casa actif) ;
     décocher le stockage local s'il n'est pas voulu (il consomme le disque de la VM) ;
   - rétention : 7 sauvegardes (Home Assistant Cloud n'en garde qu'une, la plus récente,
     avec une taille maximale imposée par Nabu Casa).
3. **Clé de chiffrement** : Home Assistant chiffre les sauvegardes automatiques. Télécharger
   le kit d'urgence et stocker la clé dans Bitwarden ; sans elle, aucune restauration
   n'est possible, ni depuis Synology ni depuis Nabu Casa. Ne jamais la recopier ici.

**Contrôles attendus après configuration** (à dater dans ce fichier) : une sauvegarde
manuelle apparaît dans les deux emplacements ; le fichier `.tar` est présent dans le
dossier du NAS ; la clé du kit d'urgence ouvre la sauvegarde (restauration de test, voir
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
3. Restaurer (VMID libre pour un test, 104 pour une reprise) :

   ```sh
   # Test : nouvelle MAC, carte déconnectée, pas de démarrage automatique
   qmrestore /mnt/pve/Reminas/dump/vzdump-qemu-104-<date>.vma.zst 104 --storage local-lvm --unique 1
   qm set 104 --net0 virtio,bridge=vmbr0,link_down=1 --onboot 0
   # Reprise réelle : même commande sans --unique ni qm set
   ```

4. Démarrer, ouvrir la console (`qm terminal 104` ou noVNC), vérifier que HAOS démarre et
   que le cœur de Home Assistant répond (`ha core info`).
5. Reprise réelle seulement : arrêter définitivement `haos-prod-01` sur `pve` s'il existe
   encore, réactiver le lien réseau, vérifier MQTT, l'accès via Traefik et les intégrations.

### B. HAOS neuve et sauvegarde Home Assistant (indépendant de vzdump)

1. Créer une VM HAOS sur la tour de spare (template HAOS ou image officielle ; le module
   Terraform [`modules/haos`](../infra/terraform/modules/haos/) vise `pve` et n'a pas été
   essayé sur un autre nœud).
2. À l'accueil de Home Assistant, choisir **Restaurer depuis une sauvegarde** :
   - depuis Synology : récupérer le `.tar` dans le dossier partagé et le téléverser ;
   - depuis Nabu Casa : se connecter à Home Assistant Cloud pendant l'accueil et
     choisir la sauvegarde ;
   - saisir la clé de chiffrement (Bitwarden).
3. Mêmes vérifications et mêmes précautions réseau que ci-dessus.
4. Ansible ([`haos.yml`](../infra/ansible/playbooks/haos.yml)) peut ensuite réappliquer
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

| Étape | Statut au 14/11/2026 |
|---|---|
| Constater les sauvegardes Restic de prod depuis le 31/10 (journal du service, `snapshots`, rapports du contrôle du NAS, `forget` et `prune` du 07/11) | non vérifié : clés d'hôte SSH absentes de `dev-agent-01` |
| Restaurer depuis Restic un dossier de Mosquitto, Zigbee2MQTT et Node-RED et le comparer | dernier test le 28/10/2026 (restauration sur disque) |
| Configurer le stockage Synology et la sauvegarde Home Assistant vers Synology et Nabu Casa | cible ; état réel non vérifié (aucun accès à l'interface pendant l'audit) |
| Stocker la clé de chiffrement Home Assistant dans Bitwarden | à faire ou à confirmer |
| Créer la tâche vzdump hebdomadaire de `haos-prod-01` | cible ; absente au constat du 14/11/2026 |
| Vérifier la notification e-mail de `pve` | non vérifié |
| Restaurer `haos-prod-01` sur la tour de spare (voies A et B) | jamais exécuté ; matériel non documenté |
| Documenter la destination de Hyper Backup (copie hors site) | non documenté |
