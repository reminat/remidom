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

## Statut : création soumise à des prérequis encore ouverts

- **Traité** : la confidentialité des journaux de `tf.sh` (PR #27, fusionnée le
  05/11/2026 ; voir [Confidentialité des journaux](#confidentialité-des-journaux-traité)).
- **Ouverts** : les prérequis TLS et SSH du Mac (voir
  [Confiance TLS et SSH](#confiance-tls-et-ssh-prérequis-non-vérifiés)) et les vérifications
  Proxmox (VMID du template, mémoire, espace disque ; voir
  [Paramètres](#paramètres-à-déterminer-avant-exécution)).

La **création de la VM n'est pas utilisable** tant que ces prérequis ne sont pas vérifiés.
D'ici là, seuls les contrôles hors ligne de la racine sont utilisables (voir
[section 2](#2-création-de-la-vm)). La préparation du template ne dépend pas du wrapper.

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

### Confiance TLS et SSH (prérequis non vérifiés)

Cette racine vérifie le certificat de l'API (`pm_tls_insecure = false`). Ne jamais passer
à `true` pour contourner une erreur.

**Endpoint.** Le certificat du nœud couvre le nom `pve`, pas `pve.reminat.com` (constat du
03/11/2026, voir [Proxmox](../../docs/proxmox.md#contraintes-observées)). L'endpoint
fourni par le secret commun `proxmox_endpoint` serait donc refusé. Surcharger l'endpoint
pour cette racine seulement, dans `local.auto.tfvars` (ignoré par Git), sans modifier le
secret commun utilisé par les autres racines :

```hcl
pm_endpoint = "https://pve:8006"
```

- **Priorité** : `tf.sh` exporte `TF_VAR_pm_endpoint` depuis le secret commun ; Terraform
  lit les fichiers `*.auto.tfvars` après les variables d'environnement, donc la valeur du
  fichier local l'emporte. Vérifier dans le plan l'endpoint effectivement utilisé.
- **Adresse SSH dérivée** : le provider envoie le snippet cloud-init en SSH root vers
  l'hôte extrait de l'endpoint (`main.tf`). Avec cette surcharge, il se connecte donc à
  `pve`, et non plus à `pve.reminat.com`.

**À vérifier sur le Mac avant toute exécution** (rien n'a été vérifié ni modifié) :

| Point | Vérification |
|---|---|
| Résolution du nom | `pve` doit résoudre vers le nœud attendu (`dscacheutil -q host -a name pve`) |
| CA pour le provider (Go) | Sur macOS, un programme Go valide les certificats avec le trousseau du système et ignore `SSL_CERT_FILE` : la CA du cluster doit y être approuvée. Empreinte à comparer avec la copie du dépôt [`pve-root-ca.pem`](../ansible/roles/compose_deploy/files/pve-root-ca.pem) (`openssl x509 -noout -fingerprint -sha256`). Contrôle par le trousseau, sans authentification : `security verify-cert -c <certificat du nœud> -p ssl -s pve` |
| Confiance SSH | Une entrée `known_hosts` pour `pve` dont l'empreinte a été vérifiée par un autre canal (console Proxmox). Le comportement exact du provider vis-à-vis de `known_hosts` reste à confirmer |

Ces prérequis concernent le Mac, Bitwarden et le certificat Proxmox, qui ne sont pas
modifiés par cette PR. Alternative à plus long terme : un certificat du nœud couvrant
`pve.reminat.com` (changement Proxmox, hors périmètre).

### Confidentialité des journaux (traité)

Traité par la PR #27 : `tf.sh` n'affiche plus aucune valeur issue de Bitwarden, n'a plus de
mode debug et refuse les journaux de diagnostic de Terraform (voir
[`HOW-TO.md`, « Journaux du wrapper »](HOW-TO.md#journaux-du-wrapper)). Ces règles sont
couvertes par le harnais [`tests/tf-wrapper-test.sh`](tests/tf-wrapper-test.sh). Le
wrapper ne filtre pas ce qu'affichent Terraform et le provider : ne pas publier leurs
sorties brutes.

### Paramètres à déterminer avant exécution

| Paramètre | Où | Vérification |
|---|---|---|
| VMID du template Ubuntu 24.04 | argument du script, puis `vm_template_id` dans `local.auto.tfvars` | Libre : `pvesh get /cluster/nextid --vmid <VMID>` doit répondre ce VMID ; le script le revérifie |
| Endpoint de l'API | `pm_endpoint` dans `local.auto.tfvars` | Nom présent dans le certificat du nœud (voir ci-dessus) |
| VMID de dev-agent-02 | non fixé | Attribué par Proxmox à la création (premier libre) ; relevé dans le plan puis dans Proxmox |
| MAC (facultatif) | `vm_mac_address` dans `local.auto.tfvars` | Dédiée à dev-agent-02, jamais celle de dev-agent-01 ; sinon MAC générée |
| Mémoire disponible sur le nœud | — | 6 Go de plus ; la marge est faible (voir [Proxmox](../../docs/proxmox.md#contraintes-observées)) |
| Espace sur `local-lvm` | — | 80 Go (provisionnement fin) |

Les valeurs privées (VMID réel, endpoint, MAC) vont dans
`envs/test/pve/dev-agent-02/local.auto.tfvars`, ignoré par Git, à créer depuis
[`local.auto.tfvars.example`](envs/test/pve/dev-agent-02/local.auto.tfvars.example).

La validation Terraform n'accepte qu'un entier de 100 à 999999999 ; elle ne prouve pas que
ce VMID désigne le bon template. Vérifier dans Proxmox que la source est bien le nouveau
template Ubuntu 24.04 : `qm config <VMID>` doit montrer `template: 1`, le nom
`tpl-ubuntu-2404-cloudinit`, la description `release-AAAAMMJJ` avec sa somme, `bios: ovmf`
et `machine: q35`.

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

Les seules écritures avant la création sont locales à l'hôte : un fichier de verrou et le
cache de l'image vérifiée. Le verrou empêche deux exécutions simultanées du script, mais ne
protège pas contre d'autres outils ou l'interface Proxmox. En cas d'échec d'une commande
Proxmox, rien n'est supprimé : inspecter le VMID (`qm config <VMID>`) et décider
manuellement.

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

### Utilisable aujourd'hui : contrôles hors ligne

Sans secret, sans accès à Proxmox ni état :

```bash
cd infra/terraform/envs/test/pve/dev-agent-02
terraform init -backend=false   # sans -upgrade ; télécharge seulement le provider verrouillé
terraform fmt -check -recursive ../../../../
terraform validate
```

### Procédure future (non utilisable actuellement)

> **Ne pas exécuter** tant que les prérequis TLS et SSH du Mac ne sont pas vérifiés et que
> les vérifications Proxmox (VMID du template, mémoire, espace disque) ne sont pas faites.
> Le prérequis de confidentialité du wrapper est traité (PR #27).

```bash
cd infra/terraform/envs/test/pve/dev-agent-02
cp local.auto.tfvars.example local.auto.tfvars   # compléter : VMID du template, endpoint, MAC facultative
terraform init                                    # sans -upgrade, sans secret
cd ../../../..                                    # infra/terraform

bw login                                          # ou bw unlock
export BW_SESSION="$(bw unlock --raw)"
./tf.sh test/pve/dev-agent-02 plan -out=dev-agent-02.tfplan
```

#### Contrôles avant apply

Le plan doit montrer **uniquement** :

- `Plan: 2 to add, 0 to change, 0 to destroy` ;
- `module.dev_agent.proxmox_virtual_environment_file.user_data` (snippet
  `dev-agent-02.user-data.yaml`) et `module.dev_agent.proxmox_virtual_environment_vm.vm` ;
- nom `dev-agent-02`, clone du template Ubuntu 24.04 vérifié ci-dessus, `bios = "ovmf"`,
  `machine = "q35"`, un `efi_disk`, 4 cœurs, 6144 Mo, disque 80 Go sur `local-lvm`, pont
  `vmbr0`, `on_boot = true`.

Tout remplacement, modification ou destruction, ou toute ressource d'un autre nom :
**ne pas appliquer**.

```bash
./tf.sh test/pve/dev-agent-02 apply dev-agent-02.tfplan
rm -f envs/test/pve/dev-agent-02/dev-agent-02.tfplan   # le plan contient des valeurs sensibles
```

Fin de la validation (même condition) :

```bash
./tf.sh test/pve/dev-agent-02 destroy   # ne touche que les ressources de cette racine
```

Le template Ubuntu 24.04 n'est pas détruit par cette commande.

#### Indépendance des états

- État local `envs/test/pve/dev-agent-02/terraform.tfstate`, distinct des états Docker et
  HAOS ; aucune copie ni import d'un autre état.
- Après `apply` ou `destroy`, `tf.sh` en copie une version horodatée sur le NAS dans le
  dossier `test_pve_dev-agent-02` (voir [Sauvegardes](../../docs/sauvegardes.md#état-terraform)).
  Cette copie ne couvre pas tous les cas d'échec partiel : prévoir une copie privée de
  l'état après une opération interrompue. Ne jamais publier plans, états ou sorties brutes.
- Les modifications du module `vm` (options UEFI, disque EFI, MAC, chemin du fragment SSH)
  ont des valeurs par défaut qui laissent les racines Docker sans argument nouveau et leur
  snippet rendu identique. Ce n'est pas une preuve de plan vide ; lancer un `plan` sur ces
  racines avant leur prochain `apply`.
- Le fragment SSH de dev-agent-02 (`00-homelab.conf`) est lu avant celui de cloud-init, car
  sshd retient la première valeur d'un réglage. Vérifier les réglages effectifs dans la VM.

#### Contrôles après création

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

## Portabilité vers un autre hôte Proxmox

Hypothèses à revoir avant d'utiliser cette racine ailleurs :

- `cpu.type = "host"` (module) : la VM dépend du modèle de processeur de l'hôte ;
- stockage `local-lvm` (disques, disque EFI, cloud-init) et `local` avec le contenu
  *Snippets* ;
- pont `vmbr0` ;
- nœud `pve` (`common.auto.tfvars`) et accès SSH root pour l'envoi du snippet ;
- template : le VMID est propre à l'hôte, à recréer avec le script.
