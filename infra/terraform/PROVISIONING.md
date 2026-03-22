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

# 5. Provisionner prod
./tf.sh prod/pve/docker-host apply
./tf.sh prod/pve/haos        apply
```

Les templates Proxmox sont créés automatiquement s'ils n'existent pas.

Pour trouver les IPs après boot : UI Proxmox → VM → Summary → IP Address.

---

Pour tout le reste (prérequis, secrets Bitwarden, troubleshooting) : [HOW-TO.md](HOW-TO.md)
