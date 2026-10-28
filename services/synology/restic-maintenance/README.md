# Maintenance Restic sur le NAS Synology

Maintenance des repositories Restic exécutée **localement sur le NAS**, et non via le
REST Server (qui reste en append-only pour les clients). Le mot de passe du repository
ne quitte jamais le NAS.

**Version actuelle : dry-run uniquement.** Le script ne supprime rien, et le repository
n'est accessible qu'en lecture :

| Protection | Mécanisme |
|---|---|
| Configuration | `mode` doit valoir `dry-run`, sinon arrêt (code 2) avant tout appel à Docker ; le rôle Ansible refuse aussi toute autre valeur. Chemins et paramètres de rétention validés strictement (aucune valeur ne peut ajouter ou modifier un argument). |
| Commandes | Liste blanche (`version`, `snapshots`, `list`, `check`, `forget`) ; `forget` sans `--dry-run`, `prune`, `unlock`, `key`… sont refusés avant exécution. |
| Système de fichiers | Repository et secret montés en **lecture seule** (`:ro`, `docker_base()`) ; conteneur `--read-only`, sans réseau, `--cap-drop ALL` (+ `DAC_READ_SEARCH`), `no-new-privileges`. **C'est la protection principale.** |
| Vérification à l'exécution | Une barrière (busybox `sh`/`awk` de l'image) s'exécute **dans chaque conteneur restic**, avant restic : elle lit les montages de ce conteneur et exige `ro` sur `/`, `/repo`, le secret et **tout sous-montage de `/repo`**, sinon refus (code 97) et arrêt (`unsafe_mounts`, code 2). Un pré-contrôle identique, fait en Python sur un conteneur préalable, produit un message détaillé. Portée exacte : voir « Contrôle des montages ». |
| Restic | `forget` est toujours lancé avec `--dry-run`. `--no-lock`, ajouté à toutes les commandes, évite seulement d'écrire un fichier de verrou dans le repository (ce qu'un montage en lecture seule interdirait de toute façon) : ce n'est **pas** un équivalent de `--dry-run`. |
| Image | Référence par digest (`restic/restic@sha256:…`) avec `--pull never`, puis contrôle de `restic version` (`unexpected_version`, code 4). Le digest désigne une image précise ; il ne prouve à lui seul ni sa provenance ni son innocuité. |
| Interpréteur | Le script refuse de démarrer sans `python3 -I` : sans mode isolé, un module (`json.py`…) déposé à côté du script serait importé en root. |
| Stockage privé | État, rapports et verrou dans un dossier réservé à root hors des volumes de données, ouvert sans suivre de lien symbolique et vérifié à chaque niveau (voir « Stockage privé »). |

## Fichiers

| Repo | NAS | Propriétaire |
|---|---|---|
| `restic_maintenance.py` | `/volume1/docker/restic-maintenance/<nom>/restic_maintenance.py` | déposé par le compte Ansible (`remi`) |
| `config.json.j2` | `/volume1/docker/restic-maintenance/<nom>/config.json` | déposé par le compte Ansible |
| — | `/root/restic-maintenance/<nom>/state/` (`lock`, `state.json`) | root, 0700, créé et vérifié par le script |
| — | `/root/restic-maintenance/<nom>/logs/` (`report-*.json`) | root, 0700, créé et vérifié par le script |
| — | `/volume1/docker/restic-maintenance/secrets/` | root, **ne pas modifier** |

Droits constatés le 28/10/2026 sur `docker-test/` (`synoacltool -get`) : propriétaire
root, affiché `drwxrwxrwx+` mais régi par les ACL : écriture pour `administrators`,
`ContainerManager`, le propriétaire et root ; lecture seule pour `everyone` ; refus pour
`srv`. Les fichiers déployés ne sont donc **pas** protégés contre un administrateur ou un
processus `ContainerManager` : ils ne sont jamais exécutés directement (voir « Premier
lancement manuel »).

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

1. Lecture et validation stricte de `config.json`.
2. Ouverture du stockage privé (`private_dir`), verrou exclusif (`state/lock`) : une
   seconde exécution sort en code 75.
3. Fenêtre horaire (`window`), sauf `--ignore-window` pour un lancement manuel.
   Lecture de la référence (`state/state.json`) : avec `--init-baseline`, refus immédiat
   (`baseline_exists`, code 7, aucun conteneur lancé) si une référence valide existe déjà.
4. Contrôle des montages, puis `restic version` (version attendue), puis, dans le délai
   global `timeout_minutes` (conteneur tué au-delà) : `snapshots --json`, `list locks`,
   `check` (sans `--read-data`) et
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
   | `state_invalid` | `state.json` présent mais inutilisable (JSON invalide, plus de 1 Mio, structure ou valeurs incorrectes, `last_run` dans le futur, fichier non régulier, lien symbolique, droits ou propriétaire inattendus). Les comparaisons sont abandonnées et la référence existante **n'est pas remplacée**. |
   | `remove_count`, `remove_percent` | suppressions estimées au-delà de `max_remove_count` / `max_remove_percent`. **En dry-run, simple avertissement (`WARN`)** : rien n'étant supprimé, les candidats s'accumulent normalement d'une exécution à l'autre (environ un par jour). Ces seuils redeviendront bloquants pour une éventuelle suppression réelle. |
   | `young_removal` | un snapshot proposé à la suppression alors que `--keep-within` devrait le conserver : même règle que restic 0.19.1, groupe par groupe (voir ci-dessous). Ne doit jamais arriver ; signale une politique, une horloge ou des données incohérentes. |
   | `forget_time_invalid` | horodatage illisible dans la sortie de `forget` |
   | `min_keep` | groupe qui tomberait sous `min_keep_per_group` snapshots conservés |
   | `forget_mismatch` | `forget` ne couvre pas exactement les snapshots listés |

   **Garde `young_removal`**, alignée sur restic 0.19.1 (`internal/data/snapshot_policy.go`) :
   pour chaque groupe (hôte + chemins), la référence est le snapshot le plus récent
   **strictement antérieur** à l'exécution de `forget` (les snapshots datés dans le futur
   sont ignorés) ; la limite est `référence − keep_within`, calculée comme `time.AddDate` de
   Go (calendaire, dans le décalage horaire de la référence, débordements normalisés :
   31 mars − 1 mois = 3 mars) ; un snapshot **strictement postérieur** à la limite doit être
   conservé. Les dates sont comparées comme des instants, quel que soit leur décalage
   (+02:00 en été, +01:00 en hiver). Un groupe dont les sauvegardes sont arrêtées garde sa
   limite relative à son dernier snapshot, et non à l'heure actuelle. Si aucun snapshot
   n'est antérieur à l'exécution, toute suppression proposée est signalée. Avec
   `keep_within` nul, la garde est inactive (restic n'applique alors pas cette règle).

   La détection repose sur l'**identifiant** des snapshots mémorisé à chaque exécution
   validée (`state/state.json`), jamais sur les dates de modification des fichiers du
   repository, qui ne prouvent rien.
6. Référence, uniquement après une exécution sans anomalie :

   | Situation | Résultat |
   |---|---|
   | référence valide présente | mise à jour, statut `ok` (code 0) |
   | `state.json` absent, sans `--init-baseline` | **aucune référence créée**, statut `baseline_required` (code 6) |
   | `state.json` absent, avec `--init-baseline` | référence créée (sans jamais remplacer un fichier apparu entre-temps), statut `baseline_created` (code 0), ligne `RÉFÉRENCE CRÉÉE : …` |
   | `state.json` invalide, avec ou sans `--init-baseline` | anomalie `state_invalid` (code 1), fichier conservé tel quel |
   | uniquement des avertissements (`WARN`, par exemple `remove_count` en dry-run) | statut `ok` (code 0), référence mise à jour ; aucun e-mail DSM (« fin anormale » seulement) : relire régulièrement les rapports |
   | référence valide présente, avec `--init-baseline` | refus `baseline_exists` (code 7) dès le début, référence intacte |

   Le rapport indique aussi `baseline` : `absent`, `present`, `invalid` ou `created`.
   La perte de la référence (par exemple après une réinstallation de DSM, `/root` n'étant
   pas sauvegardé) n'est donc jamais compensée silencieusement : les lancements
   planifiés échouent en `baseline_required` jusqu'à une recréation manuelle et validée.
7. Rapport JSON dans `logs/` (90 derniers conservés) et résumé sur la sortie standard.

Codes de sortie : `0` ok ou référence créée, `1` anomalie, `2` configuration, stockage
ou montages, `3` hors fenêtre, `4` erreur restic/Docker ou version inattendue, `5` délai
dépassé, `6` référence absente, `7` référence déjà présente avec `--init-baseline`,
`75` exécution déjà en cours.

## Stockage privé

`private_dir` (par défaut `/root/restic-maintenance/<nom>`) contient `state/` et `logs/`.
Le script refuse tout chemin sous `/volumeN/` : les volumes de données sont régis par les
ACL Synology, qui peuvent donner l'écriture aux administrateurs et à `ContainerManager`.

À chaque exécution, le script :
- ouvre le chemin **niveau par niveau depuis `/`**, chaque niveau relativement au
  descripteur du précédent, avec `O_NOFOLLOW | O_DIRECTORY` : un lien symbolique, à
  n'importe quel niveau, fait échouer l'ouverture ;
- vérifie chaque niveau sur le descripteur ouvert (`fstat`, donc sans nouvelle résolution
  de chemin) : répertoire, propriétaire root, pas d'écriture pour le groupe ni les autres
  (sauf répertoire « sticky », dont les entrées de root ne peuvent être ni renommées ni
  supprimées par d'autres), aucun attribut étendu d'ACL (`system.posix_acl*` ou nom
  contenant `syno`) ; le dernier niveau (`state/` ou `logs/`) doit être exactement en
  0700 ;
- refuse un système de fichiers monté avec `synoacl` (`/proc/self/mountinfo`) ;
- crée les niveaux manquants en 0700 (`mkdir` relatif au descripteur du parent vérifié) ;
- effectue **toutes** les opérations ensuite relativement aux descripteurs vérifiés :
  verrou ouvert sans troncature ni suivi de lien (`O_NOFOLLOW`), lecture de
  `state.json` (`O_NOFOLLOW`, contrôle du type, du propriétaire, des droits, du nombre de
  liens et de la taille avant lecture), écriture via un fichier temporaire au nom
  aléatoire créé avec `O_EXCL | O_NOFOLLOW` puis `rename` dans le même répertoire, rotation
  par `unlink` relatif (un lien symbolique est supprimé, jamais suivi).

Garanties : une fois un niveau vérifié, les opérations portent sur ce répertoire précis,
même si un chemin est ensuite renommé ou remplacé (il n'y a plus de résolution de chemin
entre le contrôle et l'utilisation). Si tous les niveaux sont réservés à root, personne
d'autre ne peut d'ailleurs y créer, renommer ou remplacer quoi que ce soit.

Limites :
- un compte capable de devenir root peut tout modifier ;
- la détection des ACL repose sur des noms d'attributs étendus et sur l'option `synoacl` ;
  un mécanisme d'ACL inconnu échapperait au contrôle, d'où l'emplacement hors volume ;
- `/root` n'est pas sauvegardé par Hyper Backup : perdre `state.json` ramène à une
  nouvelle référence (`baseline` dans le rapport, à remarquer lors de la relecture) ;
- les rapports ne sont lisibles qu'avec `sudo`.

**Vérification sur DSM avant le premier lancement** (lecture seule) :
```sh
sudo stat -c '%A %a %U:%G %n' / /root          # attendu : root:root, / sans écriture groupe/autres, /root en 0700
grep -E '^[0-9]+ [0-9]+ [0-9:]+ [^ ]+ / ' /proc/self/mountinfo   # système de fichiers racine : pas de « synoacl »
sudo /usr/bin/python3 -I -c 'import os; [print(p, os.listxattr(p)) for p in ("/", "/root")]'   # attendu : aucune ACL
```
Le script refait ces contrôles à chaque exécution et refuse de démarrer si l'un échoue.

## Préparation du NAS (une fois, par un administrateur)

Ansible n'utilise pas `sudo` : ces étapes sont manuelles. Ne modifier ni les ACL, ni
`secrets/`, ni le REST Server.

1. **SSH** : le service SSH de DSM écoute sur le port 4022. Sur DSM, seuls les membres
   du groupe `administrators` peuvent se connecter en SSH.
2. **Configuration sshd** : l'authentification par clé doit être active :
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
     `authorized_keys` 0600) ;
   - détruire la copie locale de la clé privée une fois le secret créé.
4. **Clé d'hôte** : `infra/ansible/inventories/test/nas_known_hosts` contient la clé
   d'hôte ED25519 du NAS ; Ansible refuse la connexion si elle ne correspond pas
   (`ansible_host_key_checking: true`, uniquement pour le NAS). Empreinte comparée sur le
   NAS le 28/10/2026 (`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`). Si DSM
   régénère ses clés d'hôte, mettre à jour ce fichier par une PR.
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
     restic/restic@sha256:<digest> --no-lock --no-cache cat config > /dev/null && echo "secret et repository lisibles"
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

   Le digest **identifie** l'image attendue : Docker n'exécutera que l'image dont le
   contenu correspond à cette empreinte. Il ne prouve pas à lui seul sa provenance ni son
   innocuité : il a été relevé sur Docker Hub (compte `restic`), sans vérification de
   signature ; la confiance repose sur le projet restic et sur Docker Hub au moment du
   relevé. Le contrôle de `restic version` est complémentaire : il détecte un digest mis à
   jour sans la version attendue (ou l'inverse), protège le cas où le digest serait vidé
   (référence par tag, qu'un `docker tag` local peut détourner), et inscrit la version
   réellement exécutée dans chaque rapport. Il ne protège pas contre une image malveillante
   qui afficherait `0.19.1`.
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
3. Le bloc « Show reference checksums » affiche les SHA-256 de référence ; la tâche
   suivante (mode `apply`) vérifie que les fichiers présents sur le NAS leur sont
   identiques.

Le rôle ne lance ni Docker ni restic et ne crée pas la tâche DSM.

### Empreintes de référence

Les deux empreintes sont calculées **sur le runner**, avant le dépôt :
- `restic_maintenance.py` : empreinte du fichier du dépôt extrait par le workflow ;
- `config.json` : le template est rendu dans un fichier temporaire du runner
  (`delegate_to: localhost`), avec le même template et les **variables de l'hôte NAS
  utilisées pour ce déploiement** (la délégation conserve les variables de l'hôte cible).
  Le filtre `to_nice_json` trie les clés : le rendu est déterministe.

Correspondance avec le déployé : en mode `apply`, le rôle compare l'empreinte relue sur le
NAS juste après le dépôt avec la référence et échoue en cas de différence. Une différence
plus tardive (modification sur le NAS) est détectée au lancement, par la vérification de
la copie.

Hypothèses de confiance restantes : intégrité du runner `docker-test-01`, du code extrait
(commit relu) et d'Ansible ; affichage fidèle des journaux par GitHub. L'empreinte du
script peut être recalculée indépendamment sur un poste de confiance
(`git show <commit>:services/synology/restic-maintenance/restic_maintenance.py | sha256sum`)
et comparée à celle du journal ; celle de `config.json` demande Ansible pour être
reproduite hors du runner.

## Premier lancement manuel

Les fichiers déployés dans `docker-test/` sont modifiables par les administrateurs et
par `ContainerManager` (ACL). Ils ne sont jamais exécutés sur place : le bloc ci-dessous
les copie dans un répertoire temporaire réservé à root, vérifie **la copie**, puis
l'exécute en mode isolé. Il s'arrête à la première erreur et supprime toujours sa copie.

En SSH sur le NAS, avec un compte administrateur, en remplaçant les deux empreintes par
celles du journal du workflow :

```sh
REF_PY='<sha256 de restic_maintenance.py>'
REF_CFG='<sha256 de config.json>'
sudo /bin/sh -eu -s -- "$REF_PY" "$REF_CFG" <<'EOF'
SRC=/volume1/docker/restic-maintenance/docker-test
for ref in "$1" "$2"; do
  case "$ref" in ''|*[!0-9a-f]*) echo "empreinte invalide : $ref" >&2; exit 2 ;; esac
  [ "${#ref}" -eq 64 ] || { echo "empreinte invalide : $ref" >&2; exit 2; }
done
[ ! -L /root ] && [ "$(stat -c '%u %a' /root)" = "0 700" ] || { echo "/root inattendu" >&2; exit 2; }
umask 077
RUN=$(mktemp -d /root/restic-maint-run.XXXXXX)
case "$RUN" in
  /root/restic-maint-run.??????) ;;
  *) echo "chemin temporaire inattendu : $RUN" >&2; exit 2 ;;
esac
trap 'rm -rf -- "$RUN"' EXIT
trap 'exit 130' INT TERM
cp -- "$SRC/restic_maintenance.py" "$SRC/config.json" "$RUN/"
[ "$(ls -A -- "$RUN" | sort | tr '\n' ' ')" = "config.json restic_maintenance.py " ]
printf '%s  %s\n' "$1" "$RUN/restic_maintenance.py" "$2" "$RUN/config.json" | sha256sum -c -
set +e
/usr/bin/python3 -I "$RUN/restic_maintenance.py" --config "$RUN/config.json" --ignore-window --init-baseline
code=$?
echo "code de sortie : $code"
exit "$code"
EOF
```

`--init-baseline` est réservé à ce premier lancement (ou à une recréation décidée après
la perte de la référence) ; il est refusé si une référence valide existe. Pour les
lancements manuels suivants, utiliser le même bloc **sans** `--init-baseline`.

Fonctionnement :
- `sh -eu` : toute commande en échec (copie, contenu inattendu du répertoire, empreinte)
  arrête le bloc avant l'exécution ; `-u` refuse une variable non définie ;
- les empreintes sont passées en arguments (pas d'expansion dans le script) et vérifiées
  (64 caractères hexadécimaux) ;
- le répertoire temporaire est créé par `mktemp` sous `/root` (0700, root) ; son chemin est
  comparé au motif attendu **avant** l'installation du `trap`, qui ne peut donc supprimer
  que ce répertoire (`rm -rf --` ne suit pas les liens symboliques) ;
- la vérification porte sur la copie, que personne d'autre que root ne peut plus modifier ;
- le rapport se lit ensuite avec `sudo ls -l /root/restic-maintenance/docker-test/logs/`.

Limites :
- tout repose sur des empreintes de référence fiables (voir « Empreintes de référence ») ;
- un compte capable de devenir root peut contourner l'ensemble ;
- la copie ne couvre ni l'interpréteur Python, ni Docker (binaire de Container Manager et
  démon) ; l'image est désignée par son digest ;
- un fichier source remplacé par un tube nommé ferait attendre `cp` indéfiniment (blocage,
  sans exécution de code) : interrompre avec Ctrl-C, le `trap` nettoie.

**Avant le lancement : aucune sauvegarde ni maintenance en cours.** Avec `--no-lock`, le
script ne voit pas les verrous des autres processus ; une sauvegarde concurrente ne
risque rien, mais la lecture peut être incomplète ou `check` signaler des erreurs
transitoires. Contrôles en lecture seule :
```sh
# Sur le NAS
sudo ls -la --time-style=full-iso /volume1/Restic/docker-test/locks/   # attendu : vide
ps -ef | grep -i '[r]estic'                                             # seul le REST Server attendu
sudo docker ps --format '{{.Names}}  {{.Image}}  {{.Status}}' | grep -i restic
sudo netstat -tn 2>/dev/null | grep ':8100 '                            # connexions en cours (client ou Traefik)
# Sur docker-test-01 (client)
pgrep -a restic
systemctl list-timers --all | grep -i restic
crontab -l 2>/dev/null | grep -i restic; sudo grep -ri restic /etc/cron* 2>/dev/null
```
L'absence de verrou ou de connexion visible ne garantit **pas** qu'aucune sauvegarde ne
va démarrer, ni qu'aucun autre client n'écrit à distance (via Traefik ou une autre
machine) : vérifier les **planifications** de tous les clients du repository, ainsi que
les tâches Hyper Backup et les snapshots Btrfs dans DSM, puis choisir un créneau
nettement à l'écart de toutes ces exécutions.

**Validation de la référence créée** (`baseline_created`) avant de s'y fier :
- aucun constat `ERROR` ; `mounts_readonly: true` ; `restic_version: 0.19.1` ;
- `summary.per_group` cohérent avec l'état connu du repository (`core` 38 et
  `validation` 1, ou plus si des sauvegardes ont eu lieu) ;
- identifiants de la référence comparés à une liste indépendante, par exemple
  `restic snapshots` lancé avec les mêmes options de conteneur (lecture seule) :
  ```sh
  sudo cat /root/restic-maintenance/docker-test/state/state.json | python3 -c 'import sys,json; print("\n".join(i[:8] for i in json.load(sys.stdin)["snapshot_ids"]))'
  ```
- `check` sans erreur (sinon relancer hors sauvegarde avant de conclure).

Si la référence ne convient pas : la supprimer en root
(`sudo rm /root/restic-maintenance/docker-test/state/state.json`) et recommencer.

### Contrôle des montages

Deux contrôles, avec la même règle : l'option `ro` est exigée sur `/`, `/repo`,
`/run/secrets/restic_password` et sur tout point de montage situé sous `/repo/` ; pour un
point de montage répété, seule la dernière ligne (le montage visible) compte.

1. **Pré-contrôle** (`readonly_problems()`, Python) : un conteneur lancé avec les mêmes
   options affiche `/proc/self/mounts`. Il donne un diagnostic lisible et évite de lancer
   restic si la configuration de Docker est en cause.
2. **Barrière dans chaque conteneur restic** (`GATE_SCRIPT`) : le conteneur démarre par
   `/bin/sh -c`, lit **ses propres** montages avec `awk`, et ne fait `exec /usr/bin/restic`
   que si la règle est respectée ; sinon il sort en code 97 avec le marqueur
   `RESTIC_GATE_REFUSED`, sans avoir lancé restic. C'est elle qui apporte la garantie :
   le contrôle porte sur le conteneur même qui exécute restic, et non sur un conteneur
   voisin supposé identique. L'image définit `/entrypoint.sh`, qui fait seulement
   `exec nice -n 0 /usr/bin/restic` ; la barrière le remplace et appelle restic
   directement.

Pourquoi les sous-montages : avec Docker 24 (version du NAS), un montage `-v …:ro` n'est
pas récursivement en lecture seule ; un système de fichiers monté sur l'hôte sous le
repository resterait inscriptible dans le conteneur. Constaté le 28/10/2026 sur une VM
de test (Docker 28.0.4, noyau 6.8) : avec `-v …:ro`, un tmpfs monté sous la source
apparaît `ro` (Docker récent rend le montage récursif) ; avec `bind-recursive=writable`
(comportement non récursif, celui de Docker 24), il apparaît `rw` et la barrière refuse.
Passer à `--mount type=bind,…,readonly` ne réglerait pas la question sur Docker 24 (le
mode récursif `bind-recursive` n'existe qu'à partir de Docker 25) : la syntaxe actuelle
est conservée, et la barrière couvre le cas.

Limites :
- la règle ne vérifie pas l'**origine** de `/repo` (que la source est bien le repository
  configuré), seulement que tout ce qui est visible sous `/repo` est en lecture seule ;
- la barrière dépend de `/bin/sh` et de `awk` dans l'image ; en leur absence, le
  conteneur échoue (refus par défaut), sans lancer restic ;
- l'image, et donc la barrière elle-même, est désignée par son digest : une image
  différente au même digest est impossible, mais le digest ne prouve pas la provenance ;
- le démon Docker et le noyau sont de confiance ; `CAP_SYS_ADMIN` retirée et
  `no-new-privileges` empêchent le conteneur de remonter quoi que ce soit en écriture.

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

Script : le même bloc que le lancement manuel, sans `sudo` (la tâche s'exécute en root),
sans `--ignore-window` et **jamais avec `--init-baseline`** (une référence absente doit
produire `baseline_required`, code 6, donc un e-mail), avec les empreintes écrites dans
la tâche :

```sh
set -eu
REF_PY='<sha256 de restic_maintenance.py>'
REF_CFG='<sha256 de config.json>'
SRC=/volume1/docker/restic-maintenance/docker-test
[ ! -L /root ] && [ "$(stat -c '%u %a' /root)" = "0 700" ]
umask 077
RUN=$(mktemp -d /root/restic-maint-run.XXXXXX)
case "$RUN" in /root/restic-maint-run.??????) ;; *) exit 2 ;; esac
trap 'rm -rf -- "$RUN"' EXIT
trap 'exit 130' INT TERM
cp -- "$SRC/restic_maintenance.py" "$SRC/config.json" "$RUN/"
[ "$(ls -A -- "$RUN" | sort | tr '\n' ' ')" = "config.json restic_maintenance.py " ]
printf '%s  %s\n' "$REF_PY" "$RUN/restic_maintenance.py" "$REF_CFG" "$RUN/config.json" | sha256sum -c --quiet -
set +e
/usr/bin/python3 -I "$RUN/restic_maintenance.py" --config "$RUN/config.json"
exit $?
```

Les empreintes stockées dans la tâche sont modifiables par un administrateur dans DSM :
après chaque déploiement qui modifie ces fichiers, les mettre à jour à partir des
références du workflow. Mêmes limites que ci-dessus.

## Risques

- **Compte de déploiement administrateur** : DSM réserve SSH aux administrateurs, et
  `remi` dispose de droits étendus par les ACL Synology sur `/volume1/docker` et, a
  priori, sur `/volume1/Restic`. La clé `NAS_SSH_KEY` donne ces droits (sans le mot de
  passe `sudo`) : sa compromission permettrait de modifier les fichiers déployés et de
  contourner l'append-only du REST Server. Atténuations : clé dédiée et révocable,
  restreinte à `10.10.20.53` sans redirections, clé d'hôte épinglée ; fichiers déployés
  jamais exécutés sans vérification de leur copie ; état et rapports hors de portée de ce
  compte ; snapshots Btrfs et Hyper Backup comme filet. Les empreintes empêchent
  d'exécuter un fichier modifié, pas de le modifier, et ne protègent pas contre
  quelqu'un qui peut aussi altérer la référence ou devenir root. Isoler un compte de
  déploiement sans droit sur `/volume1/Restic` demanderait de modifier les ACL : décision
  à prendre ultérieurement.
- **Référence initiale** : si le repository est déjà altéré lors de la création de la
  référence, l'anomalie y est intégrée. La création n'est jamais automatique
  (`--init-baseline`, manuel) et doit être validée (voir « Premier lancement manuel »).
- **`check` sans verrou** : erreurs transitoires possibles pendant une sauvegarde.

## Concurrence

- **Sauvegardes clientes** : aucun verrou n'est pris, elles ne sont jamais bloquées. Un
  `snapshots` ou un `forget --dry-run` lancé pendant une sauvegarde peut ne pas voir le
  snapshot en cours : aucun impact en dry-run, mais un résultat moins fiable. L'absence de
  verrou visible ne prouve pas l'absence de sauvegarde à venir : se fier aux
  planifications (voir « Premier lancement manuel »).
- **Snapshots Btrfs (05:00) et Hyper Backup (06:00)** : lecture seule, donc aucun risque de
  copie incohérente. La fenêtre et le délai sont déjà en place pour les futurs modes
  destructifs, qui ne devront jamais chevaucher ces horaires.
