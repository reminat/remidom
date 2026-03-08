

# Proxmox + Terraform + Cloud-init  
## Architecture et workflow

Ce repository implémente une infrastructure **Infrastructure as Code (IaC)** pour un homelab basé sur **Proxmox VE**, avec une séparation claire des responsabilités entre :

- **Terraform** : provisionnement des VMs
- **Cloud-init** : bootstrap système (users, SSH, packages)
- **Ansible** (plus tard) : configuration continue
- **Docker / docker-compose** (plus tard) : services applicatifs

L’objectif est d’avoir une infra **reproductible**, **jetable**, sans configuration manuelle cachée.

---

## Vue d’ensemble des rôles

### Terraform
Terraform est responsable de :
- créer / détruire des VMs sur Proxmox
- définir CPU, RAM, disque, réseau
- rendre le template cloud-init
- uploader le **snippet cloud-init** vers Proxmox (`content_type = "snippets"`)

Terraform **ne fait pas** :
- gestion des users
- installation de paquets
- configuration système
- stockage des secrets

Terraform ne fait **qu’orchestrer**.

---

### Cloud-init
Cloud-init est responsable de :
- créer les users
- injecter les clés SSH (y compris break-glass)
- configurer sudo
- désactiver les mots de passe
- faire `apt update / upgrade`
- installer `qemu-guest-agent`
- configurer SSH (sécurité de base)

Cloud-init est exécuté **une seule fois** :
- au **premier boot** de la VM
- pas à chaque reboot
- pas quand on modifie le fichier YAML

➡️ Modifier un cloud-init **n’impacte jamais une VM existante**.

---

### Proxmox Snippets
Proxmox consomme les cloud-init personnalisés via des **snippets** :

- emplacement : `/var/lib/vz/snippets/`
- storage : généralement `local`
- format : YAML (`#cloud-config`)

Un snippet est **passif** :
- Proxmox ne l’exécute pas tout seul
- il est utilisé uniquement si une VM le référence

---

## Structure des fichiers

```
infra/
  proxmox/
    README.md
    snippets/
      docker-host.user-data.yaml
      docker-host.user-data.yaml.tftpl

infra/
  terraform/
    envs/
      prod/
        pve/
          docker-host/
            main.tf
            variables.tf
            versions.tf
            outputs.tf
            backend.tf
```

---

## Workflow standard (à mémoriser)

### 1. Modifier le cloud-init
Tu modifies par exemple :
```
infra/proxmox/snippets/docker-host.user-data.yaml.tftpl
```

Exemples :
- changement de structure cloud-init
- ajout d’un paquet
- changement de configuration SSH

👉 À ce stade, **rien ne change sur Proxmox**.

---

### 2. Créer (ou recréer) la VM avec Terraform

```bash
cd infra/terraform/envs/prod/pve/docker-host
./tf.sh apply
```

Terraform :
- clone le template cloud-init
- rend `docker-host.user-data.yaml.tftpl`
- upload le snippet rendu sur Proxmox
- démarre la VM

➡️ Le cloud-init est exécuté **maintenant**, au premier boot.

---

### 3. Modifier le cloud-init après coup
Si tu modifies le cloud-init **après** la création de la VM :

- ❌ la VM existante **ne change pas**
- ✅ il faut **détruire et recréer** la VM

```bash
./tf.sh destroy
./tf.sh apply
```

C’est volontaire.  
C’est la base d’une infra propre.

---

## Gestion des accès

### Clés SSH
- 1 workstation = 1 clé SSH
- + 1 clé **break-glass** (secours)

Les clés **publiques** sont stockées dans Bitwarden Secrets Manager avec la clé :
- `ssh_authorized_keys_json`
- format : JSON array (liste de strings)

Les clés privées :
- ne vont jamais dans Git
- sont stockées localement ou dans un coffre

Exemple de valeur `ssh_authorized_keys_json` :
```json
[
  "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA... remi@macmini",
  "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA... breakglass-remidom"
]
```

---

### Root et sudo
- pas de mot de passe root
- root interdit en SSH
- accès root via `sudo -i`
- sudo sans mot de passe (homelab)

---

## Terraform state

Terraform utilise un **state** (`terraform.tfstate`) pour savoir :
- quelles ressources existent
- quels IDs Proxmox leur correspondent

Règles :
- le state est **persistant**
- le state n’est **jamais** versionné dans Git
- le state est stocké sur un backend (NAS par exemple)

👉 Sans state, Terraform est aveugle.

---

## Ce qui va dans Git / ce qui n’y va pas

### Versionné dans Git
- tous les fichiers `*.tf`
- `.terraform.lock.hcl`
- cloud-init YAML
- scripts shell
- documentation

### Jamais dans Git
- `terraform.tfvars`
- `terraform.tfstate`
- clés privées
- secrets API

---

## Philosophie du projet

- une VM est **jetable**
- aucune config critique à la main
- toute modification durable passe par le code
- cloud-init = bootstrap
- Ansible = configuration continue
- Terraform = infra

---

## Étapes suivantes prévues

- ajout d’Ansible pour installer Docker
- déploiement des services via docker-compose
- factorisation en modules Terraform
- backend Terraform S3 (MinIO)
