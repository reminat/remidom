# Maintenance Restic sur le NAS Synology

Maintenance des repositories Restic exécutée **localement sur le NAS**, et non via le
REST Server (qui reste en append-only pour les clients). Le mot de passe du repository
ne quitte jamais le NAS.

**Version actuelle : dry-run uniquement.** Rien n'est supprimé, et rien ne peut l'être :

| Protection | Mécanisme |
|---|---|
| Configuration | `mode` doit valoir `dry-run`, sinon le script s'arrête (code 2) avant tout appel à Docker. Le rôle Ansible refuse aussi toute autre valeur. |
| Commandes | Liste blanche (`version`, `snapshots`, `list`, `check`, `forget`) ; `forget` sans `--dry-run`, `prune`, `unlock`, `key`… sont refusés avant exécution. |
| Système de fichiers | Repository et secret montés en **lecture seule** (`:ro`, `docker_base()`) ; conteneur `--read-only`, sans réseau, `--cap-drop ALL` (+ `DAC_READ_SEARCH`), `no-new-privileges`. **C'est la protection principale.** |
| Vérification à l'exécution | Avant tout appel à restic, le script lance le même conteneur pour lire `/proc/self/mounts` et exige l'option `ro` sur `/`, `/repo` et `/run/secrets/restic_password` ; sinon arrêt (statut `unsafe_mounts`, code 2). |
| Restic | `--no-lock` : aucun verrou écrit. Restic 0.19.1 n'accepte `--no-lock` pour `forget` qu'avec `--dry-run`, et passe alors le repository en mode dry-run. |
| Image | Référence par digest (`restic/restic@sha256:…`) avec `--pull never`, puis contrôle de `restic version` (statut `unexpected_version`, code 4). |
| Interpréteur | Le script refuse de démarrer sans `python3 -I` : sans mode isolé, un module (`json.py`…) déposé à côté du script serait importé en root. |

## Fichiers

| Repo | NAS | Propriétaire |
|---|---|---|
| `restic_maintenance.py` | `/volume1/docker/restic-maintenance/<nom>/restic_maintenance.py` | déposé par le compte Ansible (`remi`) |
| `config.json.j2` | `/volume1/docker/restic-maintenance/<nom>/config.json` | déposé par le compte Ansible |
| — | `/volume1/docker/restic-maintenance/state/<nom>/` (verrou, état) | root, 0700, créé par le script (voir « Risques » : ACL) |
| — | `/volume1/docker/restic-maintenance/logs/<nom>/report-*.json` | root, 0700, créé par le script |
| — | `/volume1/docker/restic-maintenance/secrets/` | root, **ne pas modifier** |

Droits constatés le 28/10/2026 sur `docker-test/` (`synoacltool -get`) : propriétaire
root, affiché `drwxrwxrwx+` mais régi par les ACL : écriture pour `administrators`,
`ContainerManager`, le propriétaire et root ; lecture seule pour `everyone` ; refus pour
`srv`. Ces fichiers ne sont donc **pas** protégés contre un administrateur ou un processus
`ContainerManager` : voir « Premier lancement manuel ».

Configuration : `infra/ansible/roles/restic_maintenance/defaults/main.yml` (valeurs communes)
et `infra/ansible/inventories/<env>/group_vars/nas.yml` (repository, secret, groupes
attendus).

### Groupes attendus

Chaque snapshot doit appartenir à un groupe déclaré dans `restic_maintenance_groups` :
même hôte et **même ensemble exact de chemins** (comme `--group-by host,paths` de restic),
et, si `tags` est indiqué, exactement ces tags. Aucun préfixe n'est accepté :
`/srv/docker/core` n'autorise ni `/srv/docker`, ni `/srv/docker/core/sub`.

```yaml
restic_maintenance_groups:
  - name: core
    host: docker-test-01
    paths: [/srv/docker/core]
    tags: [core, test]
  - name: validation              # historique, figé
    host: docker-test-01
    paths: [/tmp/restic-test]
    tags: [validation]
    frozen_ids: ["1e30db7a"]
```

Un groupe avec `frozen_ids` est **historique** : seuls ces identifiants (préfixe de
8 caractères au moins) y sont acceptés, et il n'est soumis ni au contrôle de fraîcheur ni
à celui de fréquence. Il ne masque donc pas l'arrêt des sauvegardes régulières et ne
compte pas dans leurs rafales. La disparition d'un identifiant figé est une anomalie.

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
   | `host_not_allowed`, `path_not_allowed` | snapshot hors des groupes attendus (hôte inconnu, ou chemins différents) |
   | `tags_mismatch` | tags différents de ceux déclarés pour le groupe |
   | `frozen_group_changed`, `frozen_snapshot_missing` | nouveau snapshot dans un groupe historique, ou identifiant figé absent |
   | `future_snapshot` | snapshot daté de plus de `max_future_skew_minutes` dans le futur. Restic l'ignore pour `--keep-within`, mais il consomme les compteurs daily/weekly/monthly. |
   | `no_snapshot`, `stale` | groupe régulier sans snapshot de moins de `max_age_hours` |
   | `burst` | plus de `max_snapshots_per_24h` snapshots d'un même groupe régulier sur 24 h glissantes, sur tout l'historique |
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

Ansible n'utilise pas `sudo` : ces étapes sont manuelles. Ne modifier ni les ACL, ni
`secrets/`, ni le REST Server.

1. **SSH** : le service SSH de DSM écoute sur le port 4022. Sur DSM, seuls les membres
   du groupe `administrators` peuvent se connecter en SSH.
2. **Configuration sshd** : l'authentification par clé doit être active et un sous-système
   SFTP déclaré :
   ```sh
   sudo grep -iE '^\s*(PubkeyAuthentication|AuthorizedKeysFile|StrictModes|Subsystem|AllowUsers|AllowGroups|Match)' /etc/ssh/sshd_config
   ```
3. **Clé SSH dédiée au NAS** : générer une paire réservée à ce déploiement (ne pas
   réutiliser `ANSIBLE_SSH_KEY`, partagée avec les autres hôtes) :
   ```sh
   ssh-keygen -t ed25519 -N '' -C nas-restic-deploy -f nas_restic_deploy
   ```
   - clé privée → secret **`NAS_SSH_KEY` de l'environnement GitHub `test`**
     (*Settings → Environments → test → Environment secrets*), pas un secret de dépôt ;
   - clé publique → `/var/services/homes/remi/.ssh/authorized_keys` (service « Accueil
     utilisateur » activé), restreinte au runner de test (`docker-test-01`) :
     ```
     from="10.10.20.53",no-agent-forwarding,no-port-forwarding,no-X11-forwarding,no-pty ssh-ed25519 AAAA... nas-restic-deploy
     ```
   - avec `StrictModes`, sshd ignore la clé si le répertoire personnel ou `.ssh` est
     accessible en écriture au groupe ou aux autres : vérifier
     `ls -ld /var/services/homes/remi /var/services/homes/remi/.ssh` et
     `ls -l /var/services/homes/remi/.ssh/authorized_keys` (attendu : `.ssh` 0700,
     `authorized_keys` 0600).
   - détruire la copie locale de la clé privée une fois le secret créé.
4. **Clé d'hôte** : `infra/ansible/inventories/test/nas_known_hosts` contient la clé
   d'hôte ED25519 du NAS ; Ansible refuse la connexion si elle ne correspond pas
   (`ansible_host_key_checking: true`, uniquement pour le NAS). Avant le premier
   déploiement, **comparer l'empreinte** indiquée dans ce fichier avec celle du NAS :
   ```sh
   ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
   ```
   Si DSM régénère ses clés d'hôte, mettre à jour ce fichier par une PR.
5. **Python et Docker** : `/usr/bin/python3 --version` (3.8 minimum),
   `ls -l /usr/local/bin/docker`, `sudo docker version --format '{{.Server.Version}}'`
   (20.10 minimum pour `--pull never`).
6. **Transfert de fichiers** : Ansible transfère les fichiers en mode `piped`
   (`ansible_ssh_transfer_method: piped` dans `group_vars/nas.yml`) : flux SSH avec `dd`
   de chaque côté, sans SFTP ni SCP. Prérequis : `dd` présent sur le runner et sur le NAS,
   et `piped` proposé par la version d'ansible-core du runner (2.17.14 constatée), ce que
   `ansible-doc -t connection ssh` confirme. Constaté le 28/10/2026 : `/usr/bin/dd`
   (coreutils 8.32) sur `docker-test-01`, `/usr/bin/dd` (coreutils 8.30) sur le NAS.

   SFTP n'est pas utilisé : lors du test du 28/10/2026 depuis `docker-test-01`, la
   connexion SSH a réussi mais `sftp` a échoué avec
   `subsystem request failed on channel 0`. La cause n'a pas été établie (service SFTP
   désactivé dans DSM, absence de sous-système `sftp` dans la configuration de sshd, ou
   autre restriction) ; la configuration du NAS n'a pas été modifiée. Si SFTP est activé
   plus tard, la ligne `ansible_ssh_transfer_method` peut être retirée.
7. **Répertoire de déploiement** : le créer sans modifier les ACL, puis vérifier que
   `remi` peut y écrire par héritage :
   ```sh
   sudo mkdir /volume1/docker/restic-maintenance/docker-test
   test -w /volume1/docker/restic-maintenance/docker-test && echo inscriptible   # en tant que remi
   ```
   Le rôle ne fixe pas de `mode` : sur un dossier partagé en mode ACL, `chmod` peut
   échouer ou être sans effet ; les fichiers déposés (script, configuration) ne
   contiennent aucun secret et héritent des ACL.
8. **Lecture du secret et du repository** avec exactement les restrictions du script
   (sans afficher le secret) :
   ```sh
   sudo docker run --rm --pull never --network none --read-only \
     --tmpfs /tmp:rw,noexec,nosuid,size=512m \
     --cap-drop ALL --cap-add DAC_READ_SEARCH --security-opt no-new-privileges \
     -v /volume1/Restic/docker-test:/repo:ro \
     -v /volume1/docker/restic-maintenance/secrets/restic_password_test:/run/secrets/restic_password:ro \
     -e RESTIC_REPOSITORY=/repo -e RESTIC_PASSWORD_FILE=/run/secrets/restic_password \
     restic/restic:0.19.1 --no-lock --no-cache cat config > /dev/null && echo "secret et repository lisibles"
   ```
   En cas de `permission denied`, décider entre ajouter `DAC_OVERRIDE` (les montages
   restent en lecture seule) et ajuster l'ACL.
9. **Image** : le script utilise `--pull never` et la référence par digest définie dans
   `defaults/main.yml` (`restic_maintenance_image_repo`, `restic_maintenance_restic_version`,
   `restic_maintenance_image_digest`). Ce digest est celui de l'**index multi-architecture
   publié sur Docker Hub** pour `restic/restic:0.19.1` : il est identique sur toute
   installation (vérifié le 28/10/2026 sur Docker Hub et sur l'image locale du NAS).
   Pour vérifier que l'image locale correspond et fonctionne sans téléchargement :
   ```sh
   sudo docker pull restic/restic:0.19.1          # si l'image est absente
   sudo docker image inspect restic/restic@sha256:<digest> --format '{{.Id}} {{.RepoTags}}'
   sudo docker run --rm --pull never --network none --read-only --cap-drop ALL \
     --security-opt no-new-privileges restic/restic@sha256:<digest> version
   ```
   Résultat constaté le 28/10/2026 (Docker 24.0.2) : `restic 0.19.1 compiled with go1.26.4
   on linux/amd64`. Pour changer de version : modifier la version **et** le digest dans la
   même PR, après le même contrôle.

   Le digest garantit le contenu exact de l'image. Le contrôle de `restic version` est
   complémentaire : il détecte un digest mis à jour sans la version attendue (ou
   l'inverse), protège le cas où le digest serait vidé (référence par tag, qu'un
   `docker tag` local peut détourner), et inscrit la version réellement exécutée dans
   chaque rapport. Il ne protège pas contre une image malveillante qui afficherait
   `0.19.1` : seul le digest le fait.
10. **Groupes attendus** : `restic_maintenance_groups` dans `group_vars/nas.yml` (le
    playbook refuse une liste vide). En cas d'anomalie, le rapport liste les hôtes et
    chemins réellement présents (`summary.hosts`, `summary.paths`) et le nombre de
    snapshots par groupe (`summary.per_group`).

## Déploiement (GitOps, manuel)

Workflow **NAS — Restic maintenance** (`.github/workflows/nas-restic-maintenance.yml`),
déclenché uniquement à la main, indépendant du workflow `Deploy`. GitHub ne propose un
`workflow_dispatch` qu'une fois le fichier présent sur `main` : le premier usage suppose
le merge, qui ne touche pas le NAS (le `Deploy` prod déclenché par ce merge peut être
rejeté).

1. *Actions → NAS — Restic maintenance → Run workflow*, `apply` décoché :
   exécute `ansible-playbook --check --diff` (aucune écriture sur le NAS).
2. Relire le diff, puis relancer avec `apply` coché.
3. Le dernier bloc affiche les SHA-256 de `restic_maintenance.py` et `config.json`.

Le rôle ne lance ni Docker ni restic et ne crée pas la tâche DSM.

## Risques

- **Compte de déploiement administrateur** : DSM réserve SSH aux administrateurs, et
  `remi` dispose de droits étendus par les ACL Synology, y compris, a priori, sur
  `/volume1/docker/restic-maintenance` (`secrets/`, `state/`, `logs/`) et sur
  `/volume1/Restic`. La clé `NAS_SSH_KEY` donne donc ces droits : sa compromission
  permettrait de modifier les fichiers déployés, l'état de référence ou les rapports,
  et de contourner l'append-only du REST Server. Atténuations : clé dédiée et
  révocable, restreinte à `10.10.20.53` sans redirections, clé d'hôte épinglée,
  exécution depuis une copie réservée à root vérifiée contre des empreintes obtenues
  hors du NAS (qui empêche d'exécuter un fichier modifié, sans empêcher la
  modification elle-même ni protéger `state/` et `logs/`), snapshots Btrfs et Hyper
  Backup comme filet. Les empreintes ne protègent pas contre quelqu'un qui peut aussi
  modifier la référence ou devenir root. Isoler un compte de déploiement sans droit
  sur `/volume1/Restic` demanderait de modifier les ACL : décision à prendre
  ultérieurement.
- **Référence initiale** : si le repository est déjà altéré lors de la première
  exécution, l'anomalie est intégrée à la référence.
- **`check` sans verrou** : erreurs transitoires possibles pendant une sauvegarde.

## Premier lancement manuel

Les fichiers déployés dans `docker-test/` sont modifiables par les administrateurs et
par `ContainerManager` (ACL). Le lancement en root se fait donc **depuis une copie dans
un répertoire réservé à root**, vérifiée **après** la copie, puis exécutée en mode isolé.

**Empreintes de référence**, à obtenir **hors du NAS** :
- `restic_maintenance.py` : depuis le commit relu et mergé, sur un poste de confiance :
  `git show <commit>:services/synology/restic-maintenance/restic_maintenance.py | sha256sum` ;
- `config.json` : bloc « Show checksums » du workflow qui l'a déployé (calculé sur le NAS
  juste après le dépôt).

En SSH sur le NAS, avec un compte administrateur :

```sh
REF_PY=<sha256 de restic_maintenance.py>
REF_CFG=<sha256 de config.json>
SRC=/volume1/docker/restic-maintenance/docker-test

# 1. Copie dans un répertoire temporaire réservé à root (0700)
RUN=$(sudo mktemp -d /root/restic-maint.XXXXXX)
sudo cp "$SRC/restic_maintenance.py" "$SRC/config.json" "$RUN/"

# 2. Vérification de la COPIE (et non de l'original, qui peut changer entre-temps)
printf '%s  %s\n' "$REF_PY" "$RUN/restic_maintenance.py" "$REF_CFG" "$RUN/config.json" \
  | sudo sha256sum -c - || { sudo rm -rf "$RUN"; echo "EMPREINTE DIFFÉRENTE : arrêt"; }

# 3. Lancement isolé depuis la copie (seulement si l'étape 2 a affiché OK pour les deux fichiers)
sudo /usr/bin/python3 -I "$RUN/restic_maintenance.py" --config "$RUN/config.json" --ignore-window
echo "code de sortie : $?"

# 4. Rapport, puis suppression de la copie
sudo ls -l /volume1/docker/restic-maintenance/logs/docker-test/
sudo rm -rf "$RUN"
```

Ce que cette procédure apporte :
- un fichier modifié dans `docker-test/` (par un administrateur, `ContainerManager` ou la
  clé de déploiement) n'est pas exécuté : l'empreinte de la copie ne correspond plus ;
- la copie ne peut plus changer entre sa vérification et son exécution (répertoire root
  0700) ;
- `-I` ignore tout module déposé à côté du script, et le script refuse de démarrer sans.

Ses limites :
- elle ne vaut que si les empreintes de référence sont fiables : une compromission du
  dépôt GitHub ou du workflow fausserait aussi la référence ; d'où l'empreinte du script
  recalculée depuis le commit relu ;
- elle ne protège pas contre un compte capable de devenir root (un administrateur qui
  connaît son mot de passe `sudo` peut tout contourner) ;
- elle ne couvre ni l'interpréteur Python, ni le binaire Docker de Container Manager, ni
  le démon Docker, ni `state/` et `logs/` (également modifiables via les ACL : la
  référence des snapshots peut être altérée) ; l'image est couverte par son digest.

**Absence d'écriture dans le repository** : elle repose sur le montage `:ro` du
repository et du secret dans `docker_base()`, avec `--read-only` pour le reste du
conteneur, et sur la vérification à l'exécution de `/proc/self/mounts` faite par le script
avant tout appel à restic (rapport : `mounts_readonly: true`). Le statut
`unsafe_mounts` arrête tout. `--no-lock` et `--dry-run` sont une seconde barrière, propre
à restic.

Points à vérifier lors de ce premier lancement :
- le conteneur lit le repository et le secret malgré `--cap-drop ALL` (constaté lors des
  tests manuels du 28/10/2026) ;
- `check` sans verrou peut signaler des erreurs transitoires si une sauvegarde tourne en
  même temps : relancer hors sauvegarde avant de conclure ;
- le rapport : `mounts_readonly`, `restic_version`, `summary.per_group` (attendu
  `core` 38 et `validation` 1, ou plus si une sauvegarde a eu lieu), estimation des
  suppressions par groupe ; la première exécution crée la référence (`baseline`).

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

Script, selon la même procédure que le lancement manuel (copie réservée à root,
vérification de la copie, mode isolé) :

```sh
SRC=/volume1/docker/restic-maintenance/docker-test
RUN=$(mktemp -d /root/restic-maint.XXXXXX) || exit 2
trap 'rm -rf "$RUN"' EXIT
cp "$SRC/restic_maintenance.py" "$SRC/config.json" "$RUN/" || exit 2
printf '%s  %s\n' \
  '<sha256 restic_maintenance.py>' "$RUN/restic_maintenance.py" \
  '<sha256 config.json>' "$RUN/config.json" | sha256sum -c --quiet - || exit 2
/usr/bin/python3 -I "$RUN/restic_maintenance.py" --config "$RUN/config.json"
```

Les empreintes sont stockées dans la tâche DSM (configuration système, modifiable par
un administrateur dans DSM) : après chaque déploiement qui modifie ces fichiers, les
mettre à jour à partir de références obtenues hors du NAS. Mêmes limites que ci-dessus.

## Concurrence

- **Sauvegardes clientes** : aucun verrou n'est pris, elles ne sont jamais bloquées. Un
  `snapshots` ou un `forget --dry-run` lancé pendant une sauvegarde peut ne pas voir le
  snapshot en cours : aucun impact en dry-run.
- **Snapshots Btrfs (05:00) et Hyper Backup (06:00)** : lecture seule, donc aucun risque de
  copie incohérente. La fenêtre et le délai sont déjà en place pour les futurs modes
  destructifs, qui ne devront jamais chevaucher ces horaires.
