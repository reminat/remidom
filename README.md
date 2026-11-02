# remidom

Infrastructure as Code d'un homelab personnel basé sur Proxmox VE : provisionnement des VM
avec Terraform, configuration avec Ansible, services en Docker Compose, domotique avec
Home Assistant OS. Deux environnements, `test` et `prod`, issus du même code.

> Dépôt **public** : aucun secret n'y est stocké. Les secrets sont lus dans Bitwarden au
> moment de l'exécution.

## Arborescence

```
.
├── docs/                         Documentation d'ensemble (commencer par docs/index.md)
├── infra/
│   ├── terraform/                VM Proxmox : modules/, envs/<env>/pve/<machine>/, tf.sh
│   ├── ansible/                  Inventaires test/prod, playbooks, rôles, deploy.sh
│   └── proxmox/                  Scripts de templates, bootstrap HAOS, snippets cloud-init
├── services/
│   ├── docker/core/              Pile Docker : Traefik, Mosquitto, Zigbee2MQTT, Node-RED, Portainer
│   ├── ha/config/                configuration.yaml de Home Assistant (modèle Jinja2)
│   └── synology/restic-maintenance/  Maintenance Restic exécutée sur le NAS
└── .github/workflows/            Scan de secrets, déploiement test/prod, maintenance NAS
```

## Démarrage rapide

```bash
# Provisionner les VM (depuis infra/terraform, Bitwarden déverrouillé)
./tf.sh test/pve/docker-host apply

# Configurer et déployer (depuis infra/ansible)
./deploy.sh test
```

En pratique, les déploiements passent par GitHub Actions : test automatiquement depuis
une branche `feature/**`, prod manuellement depuis `main` après approbation.

## Documentation

- [Sommaire de la documentation](docs/index.md)
- [Architecture générale](docs/architecture.md)
- [Index des procédures](docs/procedures/index.md)
- [Index des décisions techniques](docs/decisions/index.md)
- Guides détaillés : [Terraform](infra/terraform/HOW-TO.md),
  [Ansible](infra/ansible/HOWTO.md), [Proxmox et cloud-init](infra/proxmox/README.md),
  [Maintenance Restic](services/synology/restic-maintenance/README.md)

## Contribuer

Règles communes aux humains et aux agents : [AGENTS.md](AGENTS.md). Consignes propres à
Claude Code : [CLAUDE.md](CLAUDE.md).
