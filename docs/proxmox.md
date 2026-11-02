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
versionnés ni relevés : non vérifiés.

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

Le template HAOS n'est pas épinglé sur une version : le recréer change la version de HAOS
des futures VM.

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

Aucune sauvegarde Proxmox (vzdump, Proxmox Backup Server) n'est décrite dans le dépôt. La
reconstruction prévue repose sur Terraform, Ansible et les sauvegardes de données (voir
[Sauvegardes](sauvegardes.md)).
