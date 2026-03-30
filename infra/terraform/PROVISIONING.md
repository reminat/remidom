# Provisionnement

```bash
# 1. Déverrouiller Bitwarden
export BW_SESSION="$(bw unlock --raw)"

# 2. Se placer dans le dossier terraform
cd infra/terraform

# 3. Init (première fois uniquement)
cd envs/test/pve/docker-host && terraform init && cd -
cd envs/test/pve/haos        && terraform init && cd -
cd envs/prod/pve/docker-host && terraform init && cd -
cd envs/prod/pve/haos        && terraform init && cd -

# 4. Provisionner test
./tf.sh test/pve/docker-host apply
./tf.sh test/pve/haos        apply

# 5. Bootstrap SSH sur les VMs HAOS (une seule fois par VM)
cd infra/proxmox/scripts
./bootstrap-haos.sh test
cd -

# 6. Provisionner prod
./tf.sh prod/pve/docker-host apply
./tf.sh prod/pve/haos        apply

# 7. Bootstrap SSH sur les VMs HAOS prod
cd infra/proxmox/scripts
./bootstrap-haos.sh prod
cd -
```

Les templates Proxmox sont créés automatiquement s'ils n'existent pas.

Pour trouver les IPs après boot : UI Proxmox → VM → Summary → IP Address.

---

## Après le provisionnement : déployer avec Ansible

```bash
cd infra/ansible

# Docker hosts (test puis prod)
./deploy.sh test
./deploy.sh prod

# HAOS (une fois le bootstrap SSH effectué)
./deploy.sh test --limit haos
./deploy.sh prod --limit haos
```

---

Pour tout le reste (prérequis, secrets Bitwarden, bootstrap SSH HAOS, troubleshooting) : [HOW-TO.md](HOW-TO.md)
