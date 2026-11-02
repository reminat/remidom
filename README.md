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

## Provisionnement et déploiement

> **Attention** : ces opérations agissent sur l'infrastructure réelle. `tf.sh … apply`
> crée ou modifie des VM sur Proxmox (et `destroy` les supprime avec leurs données) ;
> `deploy.sh` reconfigure les hôtes et redémarre des services. Toujours commencer par
> `test`, relire le `plan` ou lancer `deploy.sh <env> --check --diff`, et ne jamais viser
> `prod` sans validation préalable en test.

Prérequis :

- accès SSH à Proxmox avec la clé dédiée, et aux VM cibles ;
- `terraform`, `ansible`, `bw`, `bws`, `jq`, `python3`, `rsync` installés sur le poste ;
- Bitwarden déverrouillé, avec les secrets attendus (noms dans
  [docs/securite.md](docs/securite.md#catalogue-des-secrets-noms-seulement)) ;
- pour Terraform, l'état de l'environnement présent localement (copie sur le NAS, voir
  [docs/sauvegardes.md](docs/sauvegardes.md#état-terraform)).

Procédures détaillées :

- provisionnement des VM : [`infra/terraform/PROVISIONING.md`](infra/terraform/PROVISIONING.md)
  et [`infra/terraform/HOW-TO.md`](infra/terraform/HOW-TO.md) ;
- configuration et déploiement : [`infra/ansible/HOWTO.md`](infra/ansible/HOWTO.md#déploiement).

En pratique, les déploiements Ansible passent par GitHub Actions : test automatiquement
au push sur une branche `feature/**`, prod uniquement par lancement manuel depuis `main`,
après approbation (voir [docs/ci-cd.md](docs/ci-cd.md)).

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
