# Poste de travail dev-agent-02 : provisionnement (validation)

Guide de la première phase du chantier « poste de travail reconstruisible » : créer une VM
**dev-agent-02** neuve, temporaire et indépendante, à partir d'un template Ubuntu 24.04
dédié. Statut : **cible, rien n'a encore été exécuté**.

- `dev-agent-01` (poste actuel) reste hors Terraform et n'est pas modifié par cette phase.
- Les templates et VM existants (Docker, HAOS) ne sont ni modifiés ni recréés.
- dev-agent-02 n'est pas sauvegardée : n'y stocker aucune donnée à conserver.

| Élément | Fichier |
|---|---|
| Template Ubuntu 24.04 (UEFI/q35, cloud-init) | [`infra/proxmox/scripts/template-ubuntu-2404.sh`](../proxmox/scripts/template-ubuntu-2404.sh) |
| Racine Terraform, état propre | [`envs/test/pve/dev-agent-02/`](envs/test/pve/dev-agent-02/) |
| Module VM (options UEFI, disque EFI, MAC) | [`modules/vm/`](modules/vm/) |
| cloud-init (même base que les hôtes Docker) | [`docker-host.user-data.yaml.tftpl`](../proxmox/snippets/docker-host.user-data.yaml.tftpl) |

## Prérequis

### Poste d'exécution (Mac)

- Terraform `>= 1.6.0`, `bw`, `bws`, `rsync`, et la clé `~/.ssh/id_ed25519_pve` (voir
  [`HOW-TO.md`, « Prérequis »](HOW-TO.md#prérequis)).
- **Checkout propre et séparé** : l'ancien checkout du Mac contient du travail non commité,
  ne pas l'utiliser ni le modifier. Cloner à nouveau, par exemple :

  ```bash
  git clone git@github.com:reminat/remidom.git ~/src/remidom-dev-agent
  cd ~/src/remidom-dev-agent && git status   # propre, sur main à jour
  ```

- `terraform init` **sans** `-upgrade` : le fichier `.terraform.lock.hcl` fixe
  `bpg/proxmox` 0.111.0, la version des autres racines.

### À corriger avant toute utilisation réelle de `tf.sh`

Constats de l'audit du 04/11/2026, à traiter dans une PR séparée (non faits ici) :

- Valider la confidentialité des journaux du wrapper et de son mode debug avant de
  réutiliser ce parcours. Ne pas lancer le wrapper actuel pour cette nouvelle racine.
- Valider la confiance SSH et TLS depuis le Mac. Cette nouvelle racine active la
  vérification TLS par défaut ; préparer une CA approuvée et un nom présent dans le
  certificat, sans désactiver TLS pour contourner une erreur.

### Paramètres à déterminer avant exécution

| Paramètre | Où | Vérification |
|---|---|---|
| VMID du template Ubuntu 24.04 | argument du script, puis `vm_template_id` dans `local.auto.tfvars` | Libre : `pvesh get /cluster/nextid --vmid <VMID>` doit répondre ce VMID ; le script le revérifie |
| VMID de dev-agent-02 | non fixé | Attribué par Proxmox à la création (premier libre) ; relevé dans le plan puis dans Proxmox |
| MAC (facultatif) | `vm_mac_address` dans `local.auto.tfvars` | Dédiée à dev-agent-02, jamais celle de dev-agent-01 ; sinon MAC générée |
| Mémoire disponible sur le nœud | — | 6 Go de plus ; la marge est faible (voir [Proxmox](../../docs/proxmox.md#contraintes-observées)) |
| Espace sur `local-lvm` | — | 80 Go (provisionnement fin) |

Les valeurs privées (VMID réel, MAC) vont dans `envs/test/pve/dev-agent-02/local.auto.tfvars`,
ignoré par Git, à créer depuis [`local.auto.tfvars.example`](envs/test/pve/dev-agent-02/local.auto.tfvars.example).
La validation refuse un VMID non entier, hors plage, ou égal à 106, 9000 ou 9100.
Le template 24.04 doit être vérifié séparément : un numéro valide ne prouve pas son contenu.

## 1. Préparation du template (une fois)

Étape distincte du provisionnement : `tf.sh` ne la déclenche pas (pas de `tf.conf` dans
cette racine).

Le script :

- télécharge l'image cloud Ubuntu 24.04 amd64 **datée** (`release-20261017`) et la vérifie
  avec une somme SHA-256 épinglée dans le script ;
- refuse un VMID ou un nom déjà utilisés, un stockage ou un pont absent ; ne supprime ni
  ne modifie jamais une VM existante ;
- crée la VM en q35/OVMF avec disque EFI (`4m`, sans clés pré-enrôlées), disque système
  importé, lecteur cloud-init, agent QEMU, console série ; ne la démarre pas ; la convertit
  en template.

En cas d'arrêt pendant la création, la VM partielle **n'est pas supprimée** : l'inspecter
(`qm config <VMID>`) et décider manuellement.

```bash
# Depuis le checkout propre ; mutation sur Proxmox
scp -i ~/.ssh/id_ed25519_pve infra/proxmox/scripts/template-ubuntu-2404.sh root@pve.reminat.com:/root/
ssh -i ~/.ssh/id_ed25519_pve root@pve.reminat.com 'bash /root/template-ubuntu-2404.sh <VMID>'
```

### Changer de version d'image

Provenance de la somme épinglée : fichier `SHA256SUMS` du dossier
`https://cloud-images.ubuntu.com/releases/noble/release-20261017/`, dont la signature
`SHA256SUMS.gpg` a été vérifiée le 05/11/2026 (« UEC Image Automatic Signing Key »,
clé `D2EB44626FDDC30B513D5BB71A5D6C4C7DB87C81`, paquet `ubuntu-cloudimage-keyring`).

Pour une autre publication, refaire la même vérification puis modifier ensemble `RELEASE`
et `IMG_SHA256` :

```bash
R=https://cloud-images.ubuntu.com/releases/noble/release-AAAAMMJJ
curl -fsSLO "$R/SHA256SUMS" && curl -fsSLO "$R/SHA256SUMS.gpg"
gpgv --keyring /usr/share/keyrings/ubuntu-cloudimage-keyring.gpg SHA256SUMS.gpg SHA256SUMS
grep 'ubuntu-24.04-server-cloudimg-amd64.img$' SHA256SUMS
```

Un nouveau template prend un nouveau VMID ; l'ancien n'est pas remplacé.

## 2. Création de la VM

**Parcours futur bloqué jusqu’à validation du wrapper dans une PR séparée.**
Ne pas exécuter les commandes suivantes avec le wrapper actuel.
Les commandes de contrôle Terraform autorisées aujourd’hui se limitent à fmt et validate
sans backend ni credentials.

```bash
cd infra/terraform/envs/test/pve/dev-agent-02
cp local.auto.tfvars.example local.auto.tfvars   # puis compléter (VMID du template, MAC facultative)
terraform init                                    # sans -upgrade, sans secret
cd ../../../..                                    # infra/terraform

bw login                                          # ou bw unlock
export BW_SESSION="$(bw unlock --raw)"
./tf.sh test/pve/dev-agent-02 plan -out=dev-agent-02.tfplan
```

### Contrôles avant apply

Le plan doit montrer **uniquement** :

- `Plan: 2 to add, 0 to change, 0 to destroy` ;
- `module.dev_agent.proxmox_virtual_environment_file.user_data` (snippet
  `dev-agent-02.user-data.yaml`) et `module.dev_agent.proxmox_virtual_environment_vm.vm` ;
- nom `dev-agent-02`, clone du template Ubuntu 24.04 (pas 9000), `bios = "ovmf"`,
  `machine = "q35"`, un `efi_disk`, 4 cœurs, 6144 Mo, disque 80 Go sur `local-lvm`, pont
  `vmbr0`, `on_boot = true`.

Tout remplacement, modification ou destruction, ou toute ressource d'un autre nom :
**ne pas appliquer**.

```bash
./tf.sh test/pve/dev-agent-02 apply dev-agent-02.tfplan
rm -f envs/test/pve/dev-agent-02/dev-agent-02.tfplan   # le plan contient des valeurs sensibles
```

### Indépendance des états

- État local `envs/test/pve/dev-agent-02/terraform.tfstate`, distinct des états Docker et
  HAOS ; aucune copie ni import d'un autre état.
- Après `apply` ou `destroy`, `tf.sh` en copie une version horodatée sur le NAS dans le
  dossier `test_pve_dev-agent-02` (voir [Sauvegardes](../../docs/sauvegardes.md#état-terraform)).
- Les modifications du module `vm` (options UEFI, disque EFI, MAC) valent `null` par
  défaut : les racines Docker n'ont aucun argument nouveau. Ce n'est pas une preuve de plan
  vide ; lancer un `plan` sur ces racines avant leur prochain `apply`.

Le fragment SSH de la nouvelle instance passe avant celui généré par cloud-init.
Le chemin par défaut des autres consommateurs est conservé : leur snippet rendu reste
identique. Vérifier néanmoins les réglages effectifs dans la nouvelle VM.

### Contrôles après création

Sur Proxmox (lecture seule) :

- `qm config <VMID>` : `bios: ovmf`, `machine: q35`, `efidisk0`, `scsi0` de 80 Go,
  `cores: 4`, `memory: 6144`, `onboot: 1`, `agent` activé, `ide2` cloud-init ;
- la configuration de dev-agent-01 et des autres VM est inchangée (comparer
  `qm config` avant et après, localement).

Dans la VM (SSH avec une clé publique fournie au provisionnement) :

```bash
. /etc/os-release && echo "$PRETTY_NAME"      # Ubuntu 24.04.x LTS
nproc; free -g; lsblk                         # 4 vCPU, ~6 Go, disque ~80 Go
[ -d /sys/firmware/efi ] && echo UEFI
cloud-init status --long                      # status: done, sans erreur
systemctl is-active qemu-guest-agent ssh
hostnamectl --static                          # dev-agent-02
sudo sshd -T | grep -Ei '^(passwordauthentication|permitrootlogin) '   # no / no
```

Identités système distinctes de dev-agent-01 (comparer localement, ne rien publier) :
`/etc/machine-id`, empreintes `ssh-keygen -lf /etc/ssh/ssh_host_*_key.pub`, MAC et adresse
obtenue en DHCP.

Absences attendues : aucune clé privée utilisateur, aucun jeton (`bws`, `gh`, Claude), aucune
clé Proxmox ni jeton de déploiement dans la VM.

## 3. Configuration Ansible (future)

Hors de cette phase : dev-agent-02 n'est pas dans l'inventaire. La configuration du poste
(outils, code-server, MCP) fera l'objet d'un playbook dédié, lancé sans `deploy.sh`, qui
charge les secrets applicatifs des hôtes Docker. `code_url` et la route `code.reminat.com`
restent sur dev-agent-01.

## 4. Authentifications et secrets (futurs)

Toutes les identités de dev-agent-02 seront **nouvelles** et ne seront jamais copiées
depuis dev-agent-01 : clé SSH sortante, session GitHub, session Claude, comptes machine BWS
des MCP, jeton Proxmox d'audit. Ne jamais y installer la clé `id_ed25519_pve`, le jeton
`bws_machine_token` ni un jeton API Terraform. Seules des clés publiques entrantes sont
fournies, au provisionnement.

## 5. Sauvegarde et restauration (futures)

**Aucune sauvegarde** à ce stade : ni vzdump, ni snapshot, ni Restic. La VM se recrée par
ce guide ; ses données sont perdues à la destruction. La stratégie de sauvegarde du poste
reste à décider (voir [Sauvegardes](../../docs/sauvegardes.md)).

Fin de la validation :

```bash
./tf.sh test/pve/dev-agent-02 destroy   # ne touche que les ressources de cette racine
```

Le template Ubuntu 24.04 n'est pas détruit par cette commande.

Avant création, prévoir aussi la copie privée de l’état après une opération partiellement
échouée : la copie automatique du wrapper ne couvre pas tous ces cas. Ne jamais publier
les plans, états ou sorties brutes contenant des métadonnées privées.

## Portabilité vers un autre hôte Proxmox

Hypothèses à revoir avant d'utiliser cette racine ailleurs :

- `cpu.type = "host"` (module) : la VM dépend du modèle de processeur de l'hôte ;
- stockage `local-lvm` (disques, disque EFI, cloud-init) et `local` avec le contenu
  *Snippets* ;
- pont `vmbr0` ;
- nœud `pve` (`common.auto.tfvars`) et accès SSH root pour l'envoi du snippet ;
- template : le VMID est propre à l'hôte, à recréer avec le script.
