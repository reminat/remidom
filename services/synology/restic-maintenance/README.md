# Maintenance Restic sur le NAS Synology

Maintenance des repositories Restic exécutée **localement sur le NAS**, et non via le
REST Server (qui reste en append-only pour les clients). Le mot de passe du repository
ne quitte jamais le NAS.

**Version actuelle : dry-run uniquement.** Rien n'est supprimé, et rien ne peut l'être :

| Protection | Mécanisme |
|---|---|
| Configuration | `mode` doit valoir `dry-run`, sinon le script s'arrête (code 2) avant tout appel à Docker. Le rôle Ansible refuse aussi toute autre valeur. |
| Commandes | Liste blanche (`version`, `snapshots`, `list`, `check`, `forget`) ; `forget` sans `--dry-run`, `prune`, `unlock`, `key`… sont refusés avant exécution. |
| Système de fichiers | Repository et secret montés en **lecture seule** (`:ro`) ; conteneur `--read-only`, sans réseau, `--cap-drop ALL`. |
| Restic | `--no-lock` : aucun verrou écrit. Restic 0.19.1 n'accepte `--no-lock` pour `forget` qu'avec `--dry-run`, et passe alors le repository en mode dry-run. |

## Fichiers

| Repo | NAS | Propriétaire |
|---|---|---|
| `restic_maintenance.py` | `/volume1/docker/restic-maintenance/<nom>/restic_maintenance.py` | compte Ansible (`remi`) |
| `config.json.j2` | `/volume1/docker/restic-maintenance/<nom>/config.json` | compte Ansible |
| — | `/volume1/docker/restic-maintenance/state/<nom>/` (verrou, état) | root, 0700, créé par le script |
| — | `/volume1/docker/restic-maintenance/logs/<nom>/report-*.json` | root, 0700, créé par le script |
| — | `/volume1/docker/restic-maintenance/secrets/` | root, **ne pas modifier** |

Configuration : `infra/ansible/roles/restic_maintenance/defaults/main.yml` (valeurs communes)
et `infra/ansible/inventories/<env>/group_vars/nas.yml` (repository, secret, hôtes et
chemins autorisés).

## Déroulé d'une exécution

1. Lecture et validation de `config.json` (mode, chemins, listes autorisées, fenêtre).
2. Verrou local exclusif (`flock` sur `state/<nom>/lock`) : une seconde exécution sort en code 75.
3. Fenêtre horaire (`window`), sauf `--ignore-window` pour un lancement manuel.
4. Conteneur `restic/restic:0.19.1`, délai global `timeout_minutes` (conteneur tué au-delà) :
   `version`, `snapshots --json`, `list locks`, `check` (sans `--read-data`), puis
   `forget --dry-run --json --group-by host,paths --keep-within 30d --keep-daily 14 --keep-weekly 8 --keep-monthly 12`.
5. Contrôles (toute erreur → statut `anomaly`, code 1) :

   | Code | Contrôle |
   |---|---|
   | `host_not_allowed`, `path_not_allowed` | hôte ou chemin absent de `allowed_hosts` / `allowed_paths` |
   | `future_snapshot` | snapshot daté de plus de `max_future_skew_minutes` dans le futur. Restic l'ignore pour `--keep-within`, mais il consomme les compteurs daily/weekly/monthly. |
   | `no_snapshot`, `stale` | hôte autorisé sans snapshot de moins de `max_age_hours` |
   | `burst` | plus de `max_snapshots_per_24h` snapshots sur 24 h glissantes, sur tout l'historique |
   | `backdated_snapshot` | snapshot **nouveau** depuis la dernière exécution validée mais daté de plus de `backdate_tolerance_hours` avant elle. Un faux snapshot antidaté pourrait sinon remplacer le vrai comme « dernier du jour ». |
   | `snapshots_disappeared` | snapshot présent à la dernière exécution validée et absent maintenant (le repository est censé être append-only) |
   | `remove_count`, `remove_percent` | suppressions estimées au-delà de `max_remove_count` / `max_remove_percent` |
   | `min_keep` | groupe qui tomberait sous `min_keep_per_group` snapshots conservés |
   | `forget_mismatch` | `forget` ne couvre pas exactement les snapshots listés |

   La détection repose sur l'**identifiant** des snapshots mémorisé à chaque exécution
   validée (`state/<nom>/state.json`), jamais sur les dates de modification des fichiers
   du repository, qui ne prouvent rien. La référence n'est mise à jour qu'après une
   exécution sans anomalie ; la première exécution la crée.
6. Rapport JSON dans `logs/<nom>/` (90 derniers conservés) et résumé sur la sortie standard.

Codes de sortie : `0` ok, `1` anomalie, `2` configuration, `3` hors fenêtre, `4` erreur
restic/Docker, `5` délai dépassé, `75` exécution déjà en cours.

## Préparation du NAS (une fois, par un administrateur)

Ansible n'utilise pas `sudo` : ces étapes sont manuelles. Ne pas toucher aux
permissions de `secrets/`.

1. **SSH** : le service SSH de DSM écoute sur le port 4022. Sur DSM, seuls les membres
   du groupe `administrators` peuvent se connecter en SSH : à vérifier pour `remi`.
2. **Clé du runner** : ajouter la clé publique correspondant au secret GitHub
   `ANSIBLE_SSH_KEY` dans `/var/services/homes/remi/.ssh/authorized_keys` (service
   « Accueil utilisateur » activé), en la restreignant aux runners :
   ```
   from="10.10.20.52,10.10.20.53" ssh-ed25519 AAAA... ansible-runner
   ```
   Droits attendus : `~` sans écriture pour le groupe, `.ssh` 0700, `authorized_keys` 0600.
3. **Python** : `/usr/bin/python3 --version` doit afficher 3.8 ou plus (fourni par DSM 7).
   Les transferts Ansible utilisent SFTP ou SCP : activer SFTP dans
   *Panneau de configuration → Services de fichiers → FTP → SFTP* si nécessaire.
4. **Docker** : Container Manager installé ; `/usr/local/bin/docker` doit exister.
5. **Répertoire de déploiement**, accessible en écriture au seul compte `remi` :
   ```sh
   sudo mkdir /volume1/docker/restic-maintenance/docker-test
   sudo chown remi /volume1/docker/restic-maintenance/docker-test
   sudo chmod 0755 /volume1/docker/restic-maintenance/docker-test
   ```
   `/volume1/docker/restic-maintenance` doit rester propriété de root, et `remi` ne doit
   avoir **aucun** droit d'écriture dessus : sinon il pourrait renommer `secrets/`,
   `state/` ou `logs/`. Le dossier partagé `docker` utilise les ACL Synology : vérifier
   avec `sudo synoacltool -get <chemin>` et, si une ACL héritée donne l'écriture à
   `remi` ou à `users` sur le parent, la corriger dans File Station (*Propriétés →
   Autorisations*).
6. **Image** : la tâche utilise `--pull never`. Télécharger l'image puis relever son digest :
   ```sh
   sudo docker pull restic/restic:0.19.1
   sudo docker image inspect --format '{{index .RepoDigests 0}}' restic/restic:0.19.1
   ```
   Reporter le digest dans `restic_maintenance_image_digest` (`@sha256:…`) pour l'épingler.
7. **Hôtes et chemins autorisés** : renseigner `restic_maintenance_allowed_hosts` et
   `restic_maintenance_allowed_paths` dans `group_vars/nas.yml` (le playbook refuse des
   listes vides). Un premier lancement manuel peut aider : en cas d'anomalie, le rapport
   liste les hôtes et chemins réellement présents (`summary.hosts`, `summary.paths`).

## Déploiement (GitOps, manuel)

Workflow **NAS — Restic maintenance** (`.github/workflows/nas-restic-maintenance.yml`),
déclenché uniquement à la main, indépendant du workflow `Deploy` :

1. *Actions → NAS — Restic maintenance → Run workflow*, branche voulue, `apply` décoché :
   exécute `ansible-playbook --check --diff` (aucune écriture sur le NAS).
2. Relire le diff, puis relancer avec `apply` coché.
3. Le dernier bloc affiche les SHA-256 de `restic_maintenance.py` et `config.json`.

Le rôle ne lance ni Docker ni restic et ne crée pas la tâche DSM.

## Premier lancement manuel

En SSH sur le NAS, avec un compte administrateur :

```sh
sudo /usr/bin/python3 /volume1/docker/restic-maintenance/docker-test/restic_maintenance.py \
  --config /volume1/docker/restic-maintenance/docker-test/config.json --ignore-window
echo "code de sortie : $?"
sudo ls -l /volume1/docker/restic-maintenance/logs/docker-test/
```

Points à vérifier lors de ce premier lancement :
- le conteneur lit le repository et le secret malgré `--cap-drop ALL` : seule
  `DAC_READ_SEARCH` est rendue. Si les ACL Synology bloquent la lecture
  (`permission denied`), c'est à trancher : ajouter `DAC_OVERRIDE` (les montages restent
  en lecture seule) ou ajuster l'ACL du repository ;
- `check` sans verrou peut signaler des erreurs transitoires si une sauvegarde tourne en
  même temps : relancer hors sauvegarde avant de conclure ;
- le rapport : hôtes, chemins, estimation des suppressions par groupe.

## Tâche planifiée DSM (à créer, **désactivée**)

*Panneau de configuration → Planificateur de tâches → Créer → Tâche planifiée → Script
défini par l'utilisateur* :

| Champ | Valeur |
|---|---|
| Tâche | `restic-maintenance-docker-test (dry-run)` |
| Utilisateur | `root` |
| Activé | **décoché** |
| Planification | quotidienne, heure **à définir** dans la fenêtre `window` (après les sauvegardes, fin avant 05:00) |
| Paramètres de la tâche | « Envoyer les détails d'exécution par e-mail » → « uniquement si le script se termine anormalement » |

Script, avec vérification des empreintes affichées par le workflow (recommandé : un
fichier modifié sans mise à jour de la tâche ne sera pas exécuté en root) :

```sh
cd /volume1/docker/restic-maintenance/docker-test || exit 2
printf '%s  %s\n' \
  '<sha256 restic_maintenance.py>' restic_maintenance.py \
  '<sha256 config.json>' config.json | sha256sum -c --quiet || exit 2
exec /usr/bin/python3 restic_maintenance.py --config config.json
```

Après chaque déploiement qui modifie ces fichiers, mettre à jour les deux empreintes.

## Concurrence

- **Sauvegardes clientes** : aucun verrou n'est pris, elles ne sont jamais bloquées. Un
  `snapshots` ou un `forget --dry-run` lancé pendant une sauvegarde peut ne pas voir le
  snapshot en cours : aucun impact en dry-run.
- **Snapshots Btrfs (05:00) et Hyper Backup (06:00)** : lecture seule, donc aucun risque de
  copie incohérente. La fenêtre et le délai sont déjà en place pour les futurs modes
  destructifs, qui ne devront jamais chevaucher ces horaires.
