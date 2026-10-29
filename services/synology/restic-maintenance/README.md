# Maintenance Restic sur le NAS Synology

Maintenance des repositories Restic exécutée **localement sur le NAS**, et non via le
REST Server (qui reste en append-only pour les clients). Le mot de passe du repository
ne quitte jamais le NAS.

**Contrôle quotidien en dry-run** : il ne supprime rien, et le repository n'y est accessible
qu'en lecture (tableau ci-dessous). Les **opérations réelles** (`--apply forget`,
`--apply prune`) existent mais sont **désactivées par défaut** et passent par des tâches
DSM distinctes : voir « Opérations réelles ».

| Protection | Mécanisme |
|---|---|
| Configuration | `mode` doit valoir `dry-run`, sinon arrêt (code 2) avant tout appel à Docker ; le rôle Ansible refuse aussi toute autre valeur. Chemins et paramètres de rétention validés strictement (aucune valeur ne peut ajouter ou modifier un argument). |
| Commandes | Liste blanche (`version`, `snapshots`, `list`, `check`, `forget`, `prune`) ; `forget` et `prune` sans `--dry-run`, `unlock`, `repair`, `key`… sont refusés avant exécution. Opérations réelles : seconde liste blanche, encore plus étroite (`forget <identifiants>`, `prune --max-unused=N%`). |
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
et `infra/ansible/inventories/<env>/group_vars/nas.yml` (repository, secret, groupes ; un
fichier par environnement : `docker-test`, `docker-prod`). La connexion au NAS, identique
pour tous, est dans `infra/ansible/playbooks/group_vars/nas.yml`
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
   `check` (structure ; **lecture des données** les jours de `read_data`, voir « Vérification
   des données ») et
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
`75` exécution déjà en cours. Opérations réelles : mêmes codes (voir « Opérations réelles »).

La référence peut porter un marqueur `in_progress` (`operation`, `started`, `ids`) écrit
juste avant une opération réelle et retiré après sa vérification : tant qu'il est présent,
chaque contrôle signale `operation_interrupted` (code 1, e-mail).

## Vérification des données

Le contrôle quotidien lance `restic check` (structure : index, snapshots, arbres). Les jours
listés dans `read_data.weekdays` (par défaut **le dimanche**), le même `check` **relit en
plus les données** :

| `read_data.parts` | Option restic | Couverture |
|---|---|---|
| `1` (défaut) | `--read-data` | tous les packs référencés par l'index, à chaque fois |
| `t > 1` | `--read-data-subset=n/t` | un groupe de packs sur `t` (réparti selon l'identifiant du pack), `n` tournant chaque semaine : tout est relu en `t` semaines, y compris les packs ajoutés entre-temps |

- **Ce que cela vérifie** : chaque pack est relu depuis le disque du NAS, déchiffré et son
  empreinte contrôlée (corruption, secteurs défectueux, fichier tronqué). **Limites** : les
  packs non référencés par l'index ne sont pas relus (`check` les signale seulement) ; cela
  ne prouve pas qu'une restauration complète fonctionne (voir les tests de restauration).
- **Lecture seule** : même conteneur que le contrôle quotidien (montage `:ro`, barrière de
  montages, `--no-lock`) ; avec `--no-cache`, `check` n'écrit aucun cache, même dans `/tmp`.
  La liste blanche n'autorise pour `check` que `--read-data` ou `--read-data-subset=n/t`.
- **Charge et durée** : environ 70 Mio en prod à ce jour : quelques secondes. Le tout reste
  borné par le délai global de 120 min ; à titre indicatif, un disque de NAS relit plusieurs
  centaines de Go dans ce délai. Si le dépôt grossit au point de s'en approcher, passer à
  `parts: 4` (un quart par semaine) plutôt qu'allonger le délai.
- **Coordination** : rien de nouveau à planifier : même tâche DSM, même verrou, même fenêtre
  (03:30 test, 03:45 prod le dimanche, loin de la sauvegarde de 02:30 et avant les
  snapshots Btrfs de 05:00).
- **Erreurs** : un échec de `check` (données corrompues ou illisibles) donne le statut
  `restic_error`, code 4, donc **l'e-mail DSM existant** ; le rapport indique
  `"check": {"read_data": "--read-data", "ok": false}` et la sortie de restic. Aucune
  référence n'est mise à jour lors d'un échec.
- **Lancement manuel** : `--read-data` force une lecture intégrale pour une exécution
  (par exemple pour valider le dispositif sans attendre le dimanche).
- **En cas de pack endommagé**, restic propose `restic repair packs …` : ce script ne le
  lance jamais (commande refusée). Diagnostiquer d'abord (disque, snapshots Btrfs, copie
  Hyper Backup) ; toute réparation est une opération manuelle séparée.
- **Validé** le 28/10/2026 sur une VM jetable (restic 0.19.1, 74 Mio) : lecture intégrale en
  3 s ; après altération de 4 Kio d'un pack, le contrôle de structure répond OK mais la
  lecture des données échoue (code 4) en nommant le pack.

## Mise à jour des empreintes dans les tâches DSM

Les tâches DSM vérifient la copie de `restic_maintenance.py` et `config.json` contre des
empreintes écrites dans leur script. **Après tout déploiement qui modifie l'un de ces
fichiers**, la tâche échoue (code 1, e-mail) tant que ses empreintes ne sont pas mises à jour.
C'est voulu. Procédure, pour chaque environnement déployé :

1. Workflow **NAS — Restic maintenance** en apply : relever les deux lignes du bloc
   « Show reference checksums » et vérifier l'assertion « Fichiers déployés identiques… ».
2. Recalculer l'empreinte du script hors du NAS, depuis le commit déployé, et la comparer :
   `git show <commit>:services/synology/restic-maintenance/restic_maintenance.py | sha256sum`.
3. DSM → Planificateur de tâches → tâche `restic-maintenance-<nom> (dry-run)` (et, si elles
   existent, `(forget)` et `(prune)`) → Modifier → Paramètres de tâche : remplacer les
   valeurs de `REF_PY` et `REF_CFG` (64 caractères chacune), enregistrer.
4. Lancer le bloc manuel (sans `--init-baseline`, avec `--ignore-window`) pour vérifier les
   empreintes et le fonctionnement, ou attendre l'exécution suivante et vérifier
   l'absence d'e-mail et le rapport.

Entre le déploiement et l'étape 3, l'exécution planifiée échouerait avec un e-mail : faire
les deux dans la même journée, avant 03:30.

## Opérations réelles (`forget`, `prune`)

**Désactivées par défaut** (`restic_maintenance_apply.forget` et `.prune` à `false`) : le
script refuse alors `--apply` avant tout conteneur (`apply_disabled`, code 2). Les activer
demande une PR sur l'inventaire, un déploiement, **et** la création d'une tâche DSM dédiée
par un administrateur.

### Séparation contrôle / opération réelle

| | Contrôle quotidien | Opération réelle |
|---|---|---|
| Lancement | sans option ; tâche DSM `(dry-run)` | `--apply forget` ou `--apply prune` ; tâches DSM `(forget)` et `(prune)` distinctes |
| Condition | — | `apply.<opération>: true` dans `config.json` ; contrôle complet **sans aucune erreur** juste avant |
| Repository | lecture seule (`:ro`), `--no-lock` | lecture seule pour tout le contrôle ; **un seul conteneur** en écriture, pour la seule commande réelle, sous verrou restic normal |
| Commandes | liste blanche de lecture | `forget <identifiants complets>` ou `prune --max-unused=N%`, rien d'autre (`check_write_safe`) ; jamais `--no-lock`, `unlock`, `repair`, `--prune`, `--unsafe-*` |
| Barrière de montages | `/`, `/repo`, secret en `ro` | `/repo` exactement en `rw`, tout le reste en `ro` (même barrière, mode écriture) |

`--apply`, `--init-baseline` et `--resolve-interrupted` sont mutuellement exclusifs.

### Déroulé de `--apply forget`

1. **Contrôle complet**, identique au quotidien : montages, version, `snapshots`,
   `list locks`, `check` (lecture des données le dimanche), `forget --dry-run`, analyse.
2. **Refus** (statut `apply_refused`, code 1 ; rien n'est écrit, la référence est intacte) :

   | Demandé | Constat bloquant |
   |---|---|
   | contrôle en erreur | toute `ERROR` du contrôle quotidien ; `check` en échec (code 4) |
   | référence absente ou invalide | `baseline_required` (code 6) ; `state_invalid` |
   | sauvegardes trop anciennes | `stale`, `no_snapshot` |
   | groupe inattendu | `host_not_allowed`, `path_not_allowed`, `tags_mismatch`, `frozen_group_changed` ; `removal_not_allowed` (suppression hors groupe déclaré, dans un groupe figé, ou identifiant incomplet) |
   | volume excessif | `remove_count` (> 10), `remove_percent` (> 20 %), **bloquants ici** ; `min_keep`, `young_removal` |
   | verrou | `locks` : tout verrou présent, même ancien (**bloquant ici**, simple avertissement au quotidien) |
   | interruption précédente | `operation_interrupted` |
   | identité d'écriture | `repository_owner` (code 2) : propriétaire de `config`, `data`, `index`, `keys`, `locks`, `snapshots` différent de `apply.repository_owner` |

3. Aucun candidat : `nothing_to_remove` (code 0), aucune écriture, aucun verrou pris.
4. Montages du conteneur d'écriture vérifiés, puis **marqueur** `in_progress` écrit dans
   la référence (avec la liste des identifiants demandés).
5. `restic forget <id> <id> …` : **exactement les candidats du dry-run**, désignés par leur
   identifiant complet. La politique n'est pas réévaluée : un snapshot apparu entre-temps
   ne peut pas être supprimé. Chaque suppression est journalisée (`SUPPRIMÉ : id (date)`).
6. Vérification en lecture seule : snapshots restants = avant − demandés (rien d'autre n'a
   disparu), aucun verrou restant, `check` OK. Puis référence mise à jour, marqueur
   retiré : `forget_done` (code 0). Sinon `apply_unexpected` (code 1), marqueur conservé.

### Déroulé de `--apply prune`

Même contrôle et mêmes refus, sauf les seuils de volume des candidats de `forget` (`prune`
ne supprime aucun snapshot). Puis `prune --dry-run` (sortie dans le rapport), marqueur,
`prune --max-unused=5%`, vérification (snapshots **inchangés**, aucun verrou, `check`) :
`prune_done` (code 0). `prune` ne supprime que des données qu'aucun snapshot ne référence.

### Politique de rétention (à valider)

**Règles de restic 0.19.1** (indépendantes de toute simulation) :
`--group-by host,paths --keep-within 30d --keep-daily 14 --keep-weekly 8 --keep-monthly 12 --keep-tag keep`,
appliqué groupe par groupe ; un snapshot est conservé si **au moins une** règle le retient.

| Règle | Conserve |
|---|---|
| `keep-within 30d` | tous les snapshots strictement postérieurs à « **dernier snapshot du groupe** − 30 jours » (pas la date du jour) |
| `keep-daily 14` | le plus récent de chacun des 14 derniers **jours ayant au moins un snapshot** |
| `keep-weekly 8` | le plus récent de chacune des 8 dernières **semaines ISO (lundi–dimanche) ayant un snapshot** |
| `keep-monthly 12` | le plus récent de chacun des 12 derniers **mois ayant un snapshot** (le mois en cours compte) |
| `keep-tag keep` | tout snapshot portant le tag `keep` (voir « Protéger un snapshot ») |

Jours, semaines et mois sont pris dans le décalage horaire enregistré dans le snapshot :
ici UTC (le conteneur de sauvegarde n'a pas de fuseau). 02:30 à Paris tombe toujours le
même jour en UTC (01:30 ou 00:30), donc sans effet pratique.

**Simulation** (résultats particuliers, valables seulement sous ces hypothèses : snapshot
manuel `75eeb351` le 28/10/2026, puis exactement une sauvegarde par jour à 02:30 sans
oubli, `forget` chaque samedi) :

| Date | Conservés | Détail |
|---|---|---|
| samedi 30/01/2027 (3 mois) | **36** | les 30 quotidiens du 01/01 au 30/01/2027 ; 31/12/2026 (fin de mois), dimanches 27/12, 20/12, 13/12 (fins de semaine) ; 30/11/2026 et 31/10/2026 (fins de mois) |
| samedi 30/10/2027 (1 an) | **44** | les 30 quotidiens du 01/10 au 30/10/2027 ; 30/09 ; dimanches 26/09, 19/09, 12/09 ; fins de mois 31/08, 31/07, 30/06, 31/05, 30/04, 31/03, 28/02, 31/01/2027, 31/12 et 30/11/2026 |

- En régime établi : 43 à 44 snapshots, le plus ancien d'environ 11 mois ; 6 à 8
  suppressions par semaine (au plus 15,7 %), sous les seuils de 10 et 20 %.
- **`75eeb351` n'est pas conservé par les règles de période** : le dernier snapshot d'octobre
  2026 est celui du 31/10. Première suppression réelle vers le 28/11/2026 ; pour le garder,
  le marquer `keep` avant (voir « Protéger un snapshot ») : il s'ajoute alors aux 36 / 44.
- `keep-daily 14` est redondant tant que la sauvegarde est quotidienne : il ne compte que
  si les sauvegardes deviennent espacées.
- Ce qui change ces résultats : une sauvegarde manquée (le jour n'existe pas, rien n'est
  « rattrapé »), plusieurs sauvegardes le même jour (toutes gardées 30 jours, puis
  seulement la dernière du jour, de la semaine ou du mois), l'arrêt des sauvegardes
  (rien ne part par simple passage du temps ; `stale` bloque d'ailleurs toute suppression
  après 48 h).

### Protéger un snapshot (`keep_tags`)

`retention.keep_tags` (défaut `[keep]`) ajoute `--keep-tag keep` à la politique : restic
conserve **toujours** un snapshot portant ce tag, quelles que soient les autres règles. Le
script le vérifie aussi de son côté : un candidat portant ce tag est refusé
(`removal_not_allowed`), même si restic le proposait. Le tag s'ajoute aux tags attendus du
groupe (`core,prod` + `keep`) sans déclencher `tags_mismatch`.

**`restic tag` change l'identifiant** : restic réécrit le snapshot (même arbre, même date)
sous un nouvel identifiant, avec `original` = l'ancien, puis supprime l'ancien. Le marquage
écrit dans le repository : il se fait **sur le NAS**, pas via le REST Server (append-only,
il refuserait la suppression).

**Reconnaissance au contrôle suivant.** La référence garde, pour chaque snapshot, ses
**métadonnées** (`meta` : `tree`, `time`, `hostname`, `paths`, `tags`), écrites à chaque
exécution validée. Le remplacement d'un ancien snapshot par un nouveau n'est admis
(`INFO retagged`) que si toutes ces conditions sont réunies :

| Condition | Sinon |
|---|---|
| l'ancien identifiant a disparu, un **seul** nouveau snapshot a `original` = l'ancien | `retag_mismatch` |
| métadonnées de l'ancien présentes dans la référence | `retag_mismatch` (référence antérieure : comparaison impossible, jamais d'acceptation par défaut) |
| `tree`, `time`, `hostname`, `paths` **identiques** | `retag_mismatch` (champ modifié nommé) |
| tags = tags initiaux + au moins un tag de `keep_tags`, rien retiré, rien d'autre ajouté | `retag_mismatch` |

Un refus laisse en plus `snapshots_disappeared` et `backdated_snapshot` : la référence
n'est pas mise à jour et l'e-mail part. `original` et la présence de `keep` ne suffisent
donc jamais. **Après le déploiement**, attendre une exécution quotidienne validée (qui écrit
`meta`) avant tout marquage.

Procédure (administrateur, hors de la plage protégée et loin de 02:30, après un contrôle
quotidien `ok`), exemple pour `75eeb351` de `docker-prod` (essayer d'abord sur
`docker-test`) :

```sh
REPO=/volume1/Restic/docker-prod
SECRET=/volume1/docker/restic-maintenance/secrets/restic_password_prod
IMAGE=restic/restic@sha256:136600b6ff6843d61d355f7f71f460a166429f35de6fd11b568fece3c9a4d510
RO="sudo docker run --rm --pull never --network none --read-only --cap-drop ALL --cap-add DAC_READ_SEARCH \
  --security-opt no-new-privileges -v $REPO:/repo:ro -v $SECRET:/run/secrets/restic_password:ro \
  -e RESTIC_REPOSITORY=/repo -e RESTIC_PASSWORD_FILE=/run/secrets/restic_password $IMAGE --no-lock --no-cache"
# 1. Aucun verrou, et identifiant complet du snapshot (lecture seule)
$RO list locks | wc -l                                   # attendu : 0
$RO snapshots --json 75eeb351 | /usr/bin/python3 -I -c '
import sys, json
for s in json.load(sys.stdin): print(s["id"], s["time"], s["hostname"], s["tags"], s["tree"])'
ID='<identifiant complet, 64 caractères, relu ci-dessus>'
case "$ID" in *[!0-9a-f]*) echo "identifiant invalide";; *) [ ${#ID} -eq 64 ] && echo "identifiant OK";; esac
# 2. Marquage (écriture, verrou exclusif restic ; refus si une sauvegarde est en cours)
sudo docker run --rm --pull never --network none --read-only --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --hostname restic-tag --user 0:0 --cap-drop ALL --cap-add DAC_READ_SEARCH --cap-add DAC_OVERRIDE \
  --security-opt no-new-privileges -v $REPO:/repo -v $SECRET:/run/secrets/restic_password:ro \
  -e RESTIC_REPOSITORY=/repo -e RESTIC_PASSWORD_FILE=/run/secrets/restic_password \
  $IMAGE --no-cache tag --add keep "$ID"
# 3. Vérification : nouvel identifiant, original = ancien, même arbre, tags core,keep,prod
$RO snapshots --json --tag keep | /usr/bin/python3 -I -c '
import sys, json
for s in json.load(sys.stdin): print(s["id"], "original", s.get("original"), s["time"], s["tags"], s["tree"])'
```

`--user` : la valeur de `apply.repository_owner`. Ensuite, le bloc manuel du contrôle
(`--ignore-window`) doit donner `ok` avec `INFO retagged` ; noter le nouvel identifiant.
Retirer la protection : même commande avec `tag --remove keep` (nouvel identifiant encore).

### Accès en écriture

- **REST Server inchangé** (`--append-only --private-repos`) : `docker-prod-01` ne peut
  toujours ni supprimer ni réécrire quoi que ce soit dans son repository.
- Seule la tâche DSM (root, sur le NAS) écrit, directement sur le système de fichiers, dans
  un conteneur sans réseau où **seul** `/repo` est monté en écriture. Le mot de passe du
  repository ne quitte pas le NAS.
- Le conteneur d'écriture tourne sous `--user <apply.repository_owner>` (par défaut
  `0:0`), avec `DAC_OVERRIDE` en plus de `DAC_READ_SEARCH` (dossiers régis par les ACL
  Synology) : les fichiers créés (`prune`) ont le même propriétaire que les existants et
  restent lisibles par le REST Server. Le script vérifie ce propriétaire avant toute
  écriture (`repository_owner`, code 2).
- Le compte de déploiement peut modifier la configuration déployée, mais une
  configuration modifiée ne s'exécute pas : sa copie est vérifiée contre l'empreinte
  écrite dans la tâche DSM.

### Fréquences et planification

| Tâche DSM | Planification | Commande |
|---|---|---|
| `restic-maintenance-docker-prod (dry-run)` | quotidienne 03:45 (inchangée) | sans option |
| `restic-maintenance-docker-prod (forget)` | **hebdomadaire, samedi 04:00** | `--apply forget` |
| `restic-maintenance-docker-prod (prune)` | **mensuelle, le 1er, 04:15** | `--apply prune` |

- `forget` chaque semaine garde le volume par exécution sous les seuils (6 à 8) ; le
  samedi laisse le week-end pour réagir à un e-mail.
- `prune` une fois par mois suffit : il ne libère que de l'espace (dépôt d'environ
  70 Mio) et c'est l'opération qui réécrit des fichiers ; `--max-unused 5%` évite de
  réempaqueter pour quelques octets.
- Toutes les exécutions d'un même repository partagent le verrou `state/lock` : si l'une
  dure encore quand la suivante démarre, la seconde sort en code 75 (e-mail), sans rien
  faire. En pratique, chacune dure quelques secondes.
- **Plage protégée** `apply.protected` (défaut `04:55–08:00`, heure du NAS) : snapshots
  Btrfs (05:00) et Hyper Backup (06:00). Un décalage d'horaires ne suffit pas (délai de
  120 min, tâche retardée, lancement manuel) ; le script garantit donc :
  - aucune opération réelle ne **démarre** dans la plage, même avec `--ignore-window`
    (`protected_period`, code 3) ;
  - le délai d'une opération réelle est **raccourci** à « début de la plage − 90 s » : à
    l'échéance, `docker stop` (SIGTERM, verrou retiré par restic), SIGKILL 60 s plus
    tard ; aucun conteneur d'écriture ne tourne donc au début de la plage ;
  - refus s'il reste moins de 10 min (`protected_period`) ;
  - pas de lecture des données pendant une opération réelle (réservée au contrôle
    quotidien), pour une durée prévisible.

  Démarrée à 04:00, une opération dispose donc d'au plus 53 min ; à 04:15, 38 min.
  Le contrôle quotidien (lecture seule) n'est pas concerné. **Limite** : la fin de la plage
  (08:00) suppose que Hyper Backup se termine avant ; à ajuster à sa durée réelle.
  Après un SIGKILL, le verrou et le marqueur restent : le snapshot Btrfs ou la copie Hyper
  Backup contiennent alors un état « interrompu », récupérable comme ci-dessous.

### Coordination avec les sauvegardes

- Restic prend un **verrou exclusif** pour `forget` et `prune` ; il refuse (code 11) si un
  autre verrou existe, **même ancien**, et n'en supprime jamais aucun de lui-même (restic
  0.19.1, `checkForOtherLocks`). Le script refuse même avant (`locks`) ; un verrou apparu
  entre le contrôle et l'écriture donne `repository_locked` (code 4) : rien n'est modifié.
- Le script ne lance jamais `unlock` (refusé par les deux listes blanches).
- Une sauvegarde qui démarre **pendant** une opération réelle échoue (restic `backup` ne
  réessaie pas par défaut). Le timer de `docker-prod-01` réessaiera la nuit suivante, et
  `stale` alertera si cela se répète. D'où les horaires, loin de 02:30.

**Sauvegarde entre le dry-run et l'écriture.** Le contrôle lit sans verrou ; une sauvegarde
peut donc commencer après lui :

| Moment | Effet |
|---|---|
| en cours pendant le contrôle | verrou visible : refus `locks` |
| commencée après le contrôle, en cours à l'écriture | `forget`/`prune` ne peuvent pas prendre le verrou exclusif : `repository_locked` (code 4), **rien n'est modifié** (vérifié : code 11 de restic) |
| commencée **pendant** l'écriture | la sauvegarde échoue (verrou exclusif), le repository est intact |
| terminée entre le contrôle et l'écriture | le nouveau snapshot n'est **jamais** supprimé : `forget` ne reçoit que les identifiants du dry-run. Ces candidats restent valables : ajouter un snapshot ne peut que rendre **plus** de snapshots supprimables (la référence de `keep-within` avance, un nouveau jour, une nouvelle semaine ou un nouveau mois repousse les plus anciens), jamais en faire conserver un qui ne l'était pas. Le nouveau snapshot est journalisé (`INFO`), laissé hors de la référence, et contrôlé comme nouveau à l'exécution suivante (antidatage, groupe, tags). |

**`forget` partiel.** Restic supprime un fichier `snapshots/<id>` par snapshot demandé ; un
arrêt ou une erreur peut en laisser une partie. Garanties : seuls des identifiants
demandés peuvent disparaître (commande par identifiants, aucune politique évaluée à
l'écriture) ; le marqueur liste ces identifiants ; le contrôle suivant accepte leur absence
mais signale `operation_interrupted`, et toute autre disparition reste
`snapshots_disappeared` ; `--resolve-interrupted` refuse si une disparition sort de la
liste. À la reprise, le dry-run recalcule les candidats : supprimer des snapshots non
conservés ne change pas ceux que les règles conservent, donc seuls les restants
réapparaissent.

### Notifications et rapport

- Tâches `(forget)` et `(prune)` : « Envoyer les détails d'exécution par e-mail » →
  **toujours**. Chaque exécution envoie alors sa sortie : `SUPPRIMÉ : …` pour chaque
  snapshot, `forget : N snapshot(s) supprimé(s)…`, ou la sortie de `prune`, ou le motif du
  refus. Les échecs ont un code non nul.
- Rapport JSON : `operation` (`forget`, `prune`, `control`, `resolve-interrupted`),
  `apply.requested`, `apply.removed`, `apply.ok`, `apply.dry_run` / `apply.restic_output`
  (fin des sorties de restic), `check_after`.

| Statut | Code | Écriture |
|---|---|---|
| `forget_done`, `prune_done` | 0 | effectuée et vérifiée |
| `nothing_to_remove` | 0 | aucune |
| `apply_disabled`, `repository_owner`, `unsafe_mounts` | 2 | aucune |
| `protected_period` | 3 | aucune (plage protégée en cours ou trop proche) |
| `apply_refused`, `baseline_required` | 1 / 6 | aucune |
| `repository_locked` | 4 | aucune (restic n'a pas pu verrouiller) |
| `apply_failed`, `timeout`, `apply_unverified`, `apply_unexpected` | 4 / 5 / 4 / 1 | **peut-être partielle** : marqueur conservé |

### Validation sur un dépôt jetable

28/10/2026, VM GitHub jetable, restic 0.19.1, Python 3.8, dépôt de 40 sauvegardes
quotidiennes antidatées (207 Mio), propriétaire `0:0` :

| Scénario | Résultat |
|---|---|
| `--apply` avec la configuration par défaut | `apply_disabled` (2), aucun conteneur |
| sauvegarde en cours (verrou) | `apply_refused` (`locks`), dépôt inchangé, verrou intact ; `restic forget` direct : code 11 |
| `max_remove_count` 2 / `max_age_hours` 0 / autre propriétaire | refus `remove_count` / `stale` / `repository_owner`, dépôt inchangé |
| référence absente / snapshot d'un hôte inconnu | `baseline_required` (6) / refus `host_not_allowed` |
| `forget` arrêté par SIGTERM après la prise du verrou | `apply_failed` (4), **verrou retiré** par restic, aucun snapshot supprimé ; contrôle : `operation_interrupted` ; `--resolve-interrupted` puis reprise : `forget_done`, exactement les 7 candidats du dry-run supprimés, 33 restants |
| `prune` tué par SIGKILL après la prise du verrou | `apply_failed`, **verrou restant** ; contrôle, `--apply prune` et `--resolve-interrupted` refusés ; sauvegarde cliente refusée (code 11, `already locked exclusively`) ; `restic unlock` immédiat : sans effet ; après 31 min : `successfully removed 1 locks` ; `check` OK ; `--resolve-interrupted`, puis `prune_done` (207 → 182 Mio) |
| après maintenance | `check --read-data` OK, contrôle `ok`, nouvelle sauvegarde cliente OK, restauration identique |

### Validations restant à faire sur le NAS

Non vérifiable sur une VM (ext4, sans ACL Synology) : à faire avant toute activation en prod.

1. **Lecture seule, maintenant** : propriétaires, droits et ACL réels, et identité du REST
   Server.
   ```sh
   R=/volume1/Restic/docker-prod
   sudo stat -c '%u:%g %A %n' "$R" "$R"/config "$R"/data "$R"/index "$R"/keys "$R"/locks "$R"/snapshots
   sudo find "$R" -type f -printf '%u:%g %m\n' | sort | uniq -c        # homogénéité des fichiers
   sudo find "$R" -type d -printf '%u:%g %m\n' | sort | uniq -c
   sudo synoacltool -get "$R"; sudo synoacltool -get "$R/data"
   sudo synoacltool -get "$(sudo find "$R/data" -type f | head -n 1)"   # ACL d'un pack écrit par le REST Server
   N=$(sudo docker ps --format '{{.Names}}' | grep -i rest)
   sudo docker inspect -f 'User={{.Config.User}} Cmd={{.Config.Cmd}} Args={{.Args}}' "$N"
   sudo docker inspect -f '{{range .Mounts}}{{.Source}} -> {{.Destination}} rw={{.RW}}{{"\n"}}{{end}}' "$N"
   sudo docker exec "$N" id
   ```
   Attendu : un seul `uid:gid` pour tout le repository (`apply.repository_owner`), et le
   REST Server sous cette même identité ou root.
2. **Écriture réelle, d'abord sur `docker-test`** (même REST Server, mêmes ACL ; repository
   de test) : activer `apply` dans `inventories/test`, lancer manuellement `--apply forget`
   puis `--apply prune` sur un repository de test qui a des candidats, puis comparer les
   fichiers **réécrits** par `prune` avec ceux du REST Server :
   ```sh
   R=/volume1/Restic/docker-test
   sudo find "$R"/index "$R"/data -type f -newermt '-1 hour' -printf '%u:%g %m %p\n' | head
   sudo synoacltool -get "$(sudo find "$R/index" -type f -newermt '-1 hour' | head -n 1)"
   ```
   puis, **depuis `docker-test-01`, via le REST Server** : `restic-core snapshots`,
   `restic-core check --read-data`, une restauration dans un dossier vide et la sauvegarde
   suivante. Restic tente un `chmod` en lecture seule sur ses fichiers ; sur un dossier régi
   par les ACL Synology, vérifier que l'ACL héritée reste présente (sortie de
   `synoacltool`, identique à celle d'un pack écrit par le REST Server).
3. **Sauvegardes externes** : vérifier que les snapshots Btrfs et Hyper Backup couvrent
   bien `/volume1/Restic` (filet de récupération), et relever la durée de Hyper Backup pour
   fixer la fin de `apply.protected`.

### Activation (étape séparée, après validation de la politique)

1. Validations ci-dessus (1 et 3), puis écriture réelle sur `docker-test` (2).
2. **PR** sur `inventories/prod/group_vars/nas.yml` :
   ```yaml
   restic_maintenance_apply:
     forget: true
     prune: true
     prune_max_unused: "5%"
     repository_owner: "0:0"      # valeur relevée en 1
     protected: {start: "04:55", end: "08:00"}
   ```
3. Merge, workflow NAS `prod` (`--check`, puis apply), **mise à jour des empreintes** de la
   tâche `(dry-run)` (`config.json` change).
4. Lancements manuels (bloc de « Premier lancement manuel », chemins `docker-prod`,
   `--ignore-window`, hors de la plage protégée) : `--apply forget` (avant le 28/11/2026 :
   `nothing_to_remove`), puis `--apply prune` (verrou exclusif, éventuel nettoyage) :
   `prune_done`, aucun verrou restant ; la sauvegarde de 02:30 suivante doit réussir.
5. Tâches DSM `restic-maintenance-docker-prod (forget)` et `(prune)`, utilisateur **root**,
   créées **désactivées**, e-mail **toujours**, planification ci-dessus. Script : celui de
   la tâche `(dry-run)` (mêmes empreintes, mêmes chemins), dont la dernière commande devient :
   ```sh
   /usr/bin/python3 -I "$RUN/restic_maintenance.py" --config "$RUN/config.json" --apply forget   # ou prune
   ```
   jamais avec `--ignore-window`, `--init-baseline` ni `--resolve-interrupted`. Après
   création : vérifier `synoschedtask --get` (activée, utilisateur root, horaire) ; activer
   après un lancement manuel concluant. Après chaque déploiement, mettre à jour les
   empreintes des **trois** tâches.

Désactivation d'urgence : décocher les tâches dans DSM (immédiat), puis repasser
`apply.forget`/`apply.prune` à `false` par PR.

### Opération interrompue : récupération et limites

| Interruption | Effet sur le repository |
|---|---|
| avant l'écriture (refus, verrou d'autrui) | aucun |
| `forget` arrêté | une partie des snapshots demandés supprimée, **jamais d'autres** ; données intactes |
| `prune` arrêté | restic écrit les nouveaux packs et index **avant** de supprimer les anciens : données en double ou non référencées, **aucune perte** ; le `prune` suivant nettoie |
| délai dépassé | `docker stop` : SIGTERM, restic s'arrête et **retire son verrou** ; SIGKILL après 60 s seulement |
| SIGKILL, panne, redémarrage du NAS | le **verrou exclusif reste** : les sauvegardes échouent tant qu'il n'est pas retiré |

Dans tous les cas, le marqueur `in_progress` reste : chaque contrôle quotidien échoue en
`operation_interrupted` (e-mail) et toute nouvelle opération réelle est refusée.

Procédure (administrateur, sur le NAS) :

1. Aucun conteneur de maintenance actif : `sudo docker ps --filter name=restic-maint-`.
2. Lire le dernier rapport (`/root/restic-maintenance/docker-prod/logs/`) : opération,
   identifiants demandés, sortie de restic.
3. **Seulement si un verrou reste** (`list locks`, commande de lecture de « Préparation du
   NAS ») : attendre **30 min** après l'interruption, puis retirer les verrous **périmés**
   avec `restic unlock` (sans `--remove-all`, qui supprimerait aussi un verrou actif) :
   ```sh
   sudo docker run --rm --pull never --network none --read-only --hostname restic-unlock \
     --cap-drop ALL --cap-add DAC_READ_SEARCH --cap-add DAC_OVERRIDE --user 0:0 \
     --security-opt no-new-privileges \
     -v /volume1/Restic/docker-prod:/repo \
     -v /volume1/docker/restic-maintenance/secrets/restic_password_prod:/run/secrets/restic_password:ro \
     -e RESTIC_REPOSITORY=/repo -e RESTIC_PASSWORD_FILE=/run/secrets/restic_password \
     restic/restic@sha256:136600b6ff6843d61d355f7f71f460a166429f35de6fd11b568fece3c9a4d510 unlock
   ```
   Restic ne retire qu'un verrou de plus de 30 min ; un verrou récent est conservé.
4. Clore l'interruption avec le bloc manuel et **`--resolve-interrupted`** (lecture
   seule) : il exige un repository sans verrou, un `check` OK et que les snapshots
   disparus soient **tous** parmi ceux demandés ; il met alors la référence à jour et
   retire le marqueur (`interrupted_resolved`, code 0). Sinon il échoue sans rien changer.
5. Relancer l'opération (bloc manuel avec `--apply …`) ou attendre la prochaine exécution
   planifiée. Un `prune` suivant nettoie les restes d'un `prune` interrompu.

Limites :
- `check` en échec après une interruption : **aucune réparation automatique**
  (`repair` refusé). Diagnostiquer ; en dernier recours, restaurer le dossier
  `/volume1/Restic/docker-prod` depuis le snapshot Btrfs de 05:00 ou Hyper Backup (les
  sauvegardes postérieures sont alors perdues).
- Le marqueur est dans `/root` : perdu avec lui (réinstallation de DSM), comme la
  référence (`baseline_required`).
- Les candidats sont ceux de restic au moment du contrôle ; ils sont vérifiés (seuils,
  groupes, `young_removal`, référence) mais restent ceux de la politique configurée : une
  politique mal réglée dans une PR est la principale source de risque, d'où les seuils.

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
4. **Clé d'hôte** : `infra/ansible/nas_known_hosts` (commun aux environnements) contient la clé
   d'hôte ED25519 du NAS ; Ansible refuse la connexion si elle ne correspond pas
   (`ansible_host_key_checking: true`, uniquement pour le NAS). Empreinte comparée sur le
   NAS le 28/10/2026 (`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`). Si DSM
   régénère ses clés d'hôte, mettre à jour ce fichier par une PR.
5. **Python et Docker** : `/usr/bin/python3 --version` (3.8 minimum),
   `ls -l /usr/local/bin/docker`, `sudo docker version --format '{{.Server.Version}}'`
   (20.10 minimum pour `--pull never`).
6. **Transfert de fichiers** : Ansible transfère les fichiers en mode `piped`
   (`ansible_ssh_transfer_method: piped` dans `playbooks/group_vars/nas.yml`) : flux SSH avec `dd`
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

## Repository `docker-prod`

Même script, même rôle, même procédure que `docker-test` ; seules les valeurs de
`inventories/prod/group_vars/nas.yml` changent :

| Élément | `docker-test` | `docker-prod` |
|---|---|---|
| Repository (local au NAS) | `/volume1/Restic/docker-test` | `/volume1/Restic/docker-prod` |
| Secret sur le NAS (root, 0600) | `secrets/restic_password_test` | `secrets/restic_password_prod` (copie de `restic_password_prod`, Bitwarden) |
| Fichiers déployés | `/volume1/docker/restic-maintenance/docker-test/` | `/volume1/docker/restic-maintenance/docker-prod/` |
| Stockage privé | `/root/restic-maintenance/docker-test` | `/root/restic-maintenance/docker-prod` |
| Groupes attendus | `core` (`docker-test-01`, tags `core,test`) + `validation` figé | `core` (`docker-prod-01`, `/srv/docker/core`, tags `core,prod`) |
| Rétention évaluée, seuils, fenêtre, délai | valeurs par défaut du rôle | identiques (aucune politique propre à la prod) |
| Mode | contrôle dry-run ; opérations réelles désactivées | contrôle dry-run ; opérations réelles **désactivées** (voir « Opérations réelles ») |
| Workflow | environnement GitHub `test` (runner `docker-test-01`) | environnement GitHub `prod` (runner `docker-prod`, approbation requise) |

Le contrôle `stale` (aucun snapshot de moins de 48 h) signale aussi l'arrêt de la
sauvegarde quotidienne de `docker-prod-01` (timer 02:30), qui n'a pas de notification
propre. Avec une sauvegarde à 02:30 et un contrôle à 03:45, le seuil de 48 h **tolère une
sauvegarde manquée** : si celle du jour J échoue, le contrôle de J voit un dernier snapshot
de J−1 (25 h 15) et ne signale rien ; si celle de J+1 échoue aussi, le contrôle de J+1 voit
49 h 15 et alerte. L'alerte arrive donc au plus tard environ 25 h après le premier échec.

**Fenêtre et délai** : la fenêtre `01:00–04:30` n'est vérifiée **qu'au démarrage** (hors
fenêtre : statut `outside_window`, code 3, rien n'est lu). Une exécution commencée dans la
fenêtre va jusqu'au bout, même au-delà de 04:30, dans la limite du délai global de 120 min
(au-delà : conteneur arrêté, statut `timeout`, code 5). Démarrée à 03:45, elle se termine au
plus tard vers 05:45 ; en lecture seule, un chevauchement avec les snapshots Btrfs ou Hyper
Backup reste sans risque pour les données. Le script ne détecte pas une sauvegarde en
cours (lecture sans verrou) : il signale seulement les verrous présents (`WARN locks`) ; le
snapshot en cours peut manquer et `check` peut échouer de façon transitoire (code 4).
D'où l'heure choisie, loin de la sauvegarde de 02:30 (quelques secondes pour 81 Mo).

### Prérequis (manuels, une fois)

1. **Clé de déploiement pour l'environnement GitHub `prod`** : une paire dédiée, la clé
   privée dans le secret `NAS_SSH_KEY` de l'environnement `prod`, la clé publique dans
   `authorized_keys` de `remi` sur le NAS, restreinte au runner de prod :
   `from="10.10.20.52",no-agent-forwarding,no-port-forwarding,no-X11-forwarding,no-pty`.
2. **Dossier de déploiement** : `sudo mkdir /volume1/docker/restic-maintenance/docker-prod`,
   inscriptible par `remi` par héritage des ACL (comme `docker-test`).
3. **Secret sur le NAS**, sans l'afficher : coller la valeur de `restic_password_prod`
   (copiée depuis Bitwarden) puis Ctrl-D :
   ```sh
   sudo sh -c 'umask 077; cat > /volume1/docker/restic-maintenance/secrets/restic_password_prod'
   sudo stat -c '%a %U:%G %s octets %n' /volume1/docker/restic-maintenance/secrets/restic_password_prod
   ```
   Un saut de ligne final est sans effet (restic l'ignore).
4. **Lecture du repository avec les restrictions du script** : la commande de l'étape 8
   de « Préparation du NAS », avec `docker-prod` et `restic_password_prod`.

### Déploiement et premier lancement

1. Workflow **NAS — Restic maintenance**, environnement `prod`, `apply` décoché
   (`--check --diff`), puis coché ; empreintes de référence affichées.
2. Premier lancement manuel avec le bloc de « Premier lancement manuel », en remplaçant
   `docker-test` par `docker-prod` dans `SRC` et les empreintes par celles du workflow
   `prod`, avec `--init-baseline` ; validation de la référence (identifiants comparés à
   `restic snapshots`).
3. Tâche DSM `restic-maintenance-docker-prod (dry-run)`, même script que pour
   `docker-test` (chemins `docker-prod`), créée **désactivée**, puis activée après un
   lancement manuel concluant.

### Planification proposée

| Heure | Tâche | Repository |
|---|---|---|
| 02:30 | sauvegarde `restic-backup-core.timer` (docker-prod-01) | `docker-prod` |
| 03:02 | sauvegarde de `docker-test-01` | `docker-test` |
| 03:30 | maintenance dry-run (DSM, proposée, pas encore créée) | `docker-test` |
| **03:45** | **maintenance dry-run (DSM, active)** | `docker-prod` |
| 04:00 le samedi | `forget` réel (DSM, proposée, après activation) | `docker-prod` |
| 04:15 le 1er du mois | `prune` réel (DSM, proposée, après activation) | `docker-prod` |
| 05:00 | snapshots Btrfs | — |
| 06:00 | Hyper Backup | — |

03:45 laisse plus d'une heure après la sauvegarde de prod et reste dans la fenêtre
`01:00–04:30` ; les deux maintenances portent sur des repositories distincts et ont
chacune leur verrou.

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
  copie incohérente. Les opérations réelles se terminent normalement avant 04:30 ; même
  interrompues ou copiées en cours, le repository reste cohérent (voir « Opérations
  réelles »).
- **Opérations réelles** : verrou exclusif restic ; voir « Coordination avec les
  sauvegardes ».
