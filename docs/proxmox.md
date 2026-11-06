# Proxmox

## Hôte

| Élément | Valeur | Source |
|---|---|---|
| Nœud | `pve` (un seul nœud, pas de cluster) | Git (`common.auto.tfvars`) |
| Nom d'accès | `pve.reminat.com` (SSH, API) | Git (`tf.sh`, scripts Proxmox) |
| Réseau | VLAN 20 Servers | Git (`proxmox_url`, inventaire prod) |
| Stockage des disques de VM | `local-lvm` | Git (variables Terraform) |
| Stockage des snippets | `local`, type de contenu *Snippets* activé | Git (module `vm`), Legacy |
| Pont réseau des VM | `vmbr0` | Git (variables Terraform) |
| Accès web | `proxmox.reminat.com` via Traefik, réservé LAN et VPN | Git ([Services Docker](services-docker.md#traefik)) |

La version de Proxmox VE, le matériel et la configuration réseau de l'hôte ne sont pas
versionnés dans Git.

## État constaté (03/11/2026)

Relevé en lecture seule par le [MCP Proxmox](poste-de-travail.md#mcp-proxmox). Ponctuel :
ne garantit pas l'état ultérieur.

| Élément | Constat |
|---|---|
| Version | Proxmox VE 9.1.9 |
| Nœud | `pve` en ligne, nœud unique |
| Ressources | 8 cœurs / 16 threads, 32 Go de RAM ; mémoire utilisée à environ 82 % au relevé |
| Réseau des VM | Un seul pont, `vmbr0`, sans gestion des VLAN : aucune VM n'est étiquetée, toutes sont sur le VLAN du port du switch |
| Conteneurs LXC | Aucun |
| Snapshots | Aucun, sur aucune VM |
| Sauvegardes vzdump | Aucune tâche planifiée, aucune sauvegarde visible (voir [Sauvegardes](sauvegardes.md#vm-proxmox-vzdump)) |

### Stockages

| Stockage | Type | Contenu | Source |
|---|---|---|---|
| `local` | Répertoire | Snippets, ISO, templates, sauvegardes | Git (snippets) ; constat |
| `local-lvm` | LVM-thin | Disques des VM ; environ 100 Go utilisés sur 350 | Git (`vm_storage`) ; constat |
| `Reminas` | NFS (NAS), partagé | Tous types de contenu | **Constat seulement**, absent de Git ; héberge le disque d'une VM hors Terraform |

### Contraintes observées

- **Mémoire** : la RAM allouée aux VM actives (environ 24 Go) laisse peu de marge pour une
  VM supplémentaire ou l'augmentation d'une VM existante.
- **Certificat du nœud** : signé par la CA du cluster, il expire le **29/12/2026** ; son
  renouvellement automatique n'est pas vérifié. Il ne couvre pas le nom `pve.reminat.com`,
  ce qui explique la vérification sous le nom `pve` par Traefik et par le MCP.

### À vérifier

- Rôle du stockage `Reminas` et dépendance des VM au NAS.
- Renouvellement du certificat du nœud avant le 29/12/2026.

## Accès utilisés par l'IaC

- **API** : jeton dédié à Terraform, lu dans Bitwarden Secrets Manager par
  [`tf.sh`](../infra/terraform/tf.sh). Création du jeton :
  [`HOW-TO.md`, « Créer le token API Proxmox »](../infra/terraform/HOW-TO.md#créer-le-token-api-proxmox).
- **SSH root** avec la clé `~/.ssh/id_ed25519_pve` du poste : création des templates,
  envoi des snippets par le provider, bootstrap HAOS.
- **CA interne** : Traefik vérifie le certificat de Proxmox avec la CA publique versionnée
  dans [`roles/compose_deploy/files/pve-root-ca.pem`](../infra/ansible/roles/compose_deploy/files/pve-root-ca.pem).

## Templates

Les VM sont créées par clonage. [`tf.sh`](../infra/terraform/tf.sh) crée le template avant
un `apply` s'il est absent (paramètres dans `tf.conf` de chaque environnement).

| VMID | Nom | Contenu | Script |
|---|---|---|---|
| 9000 | `tpl-ubuntu-2204-cloudinit` | Ubuntu 22.04 cloud image, agent QEMU | [`template-docker.sh`](../infra/proxmox/scripts/template-docker.sh) |
| 9100 | `tpl-haos` | Dernière version publiée de Home Assistant OS au moment de la création, UEFI | [`template-haos.sh`](../infra/proxmox/scripts/template-haos.sh) |
| À choisir | `tpl-ubuntu-2404-cloudinit` | **Cible, non créé** : Ubuntu 24.04 cloud image datée et vérifiée, UEFI/q35, pour dev-agent-02 ; lancé à la main, jamais par `tf.sh` | [`template-ubuntu-2404.sh`](../infra/proxmox/scripts/template-ubuntu-2404.sh), voir [`DEV-AGENT.md`](../infra/terraform/DEV-AGENT.md#1-préparation-du-template-une-fois) |

Le template HAOS n'est pas épinglé sur une version : le recréer change la version de HAOS
des futures VM.

Constat du 03/11/2026 : les deux templates sont présents, sous ces VMID et ces noms.

## Snippets cloud-init

Le module Terraform `vm` rend [`docker-host.user-data.yaml.tftpl`](../infra/proxmox/snippets/docker-host.user-data.yaml.tftpl)
et l'envoie sur le stockage `local` (`/var/lib/vz/snippets/`). Prérequis, une fois : le
stockage `local` doit accepter le type de contenu *Snippets* (*Datacenter → Storage →
local → Edit → Content*).

Le fichier statique `docker-host.user-data.yaml` et le script `push-snippets.sh` datent du
premier essai (hostname figé, aucune clé) ; Terraform ne les utilise pas.

Principes de cloud-init (exécution au premier démarrage seulement, VM jetables) :
[`infra/proxmox/README.md`](../infra/proxmox/README.md).

## Sauvegardes de l'hôte et des VM

Aucune sauvegarde Proxmox (vzdump, Proxmox Backup Server) n'est décrite dans le dépôt, et
aucune n'est visible au relevé du 03/11/2026. La reconstruction prévue repose sur
Terraform, Ansible et les sauvegardes de données (voir [Sauvegardes](sauvegardes.md)).
