# CLAUDE.md

## Langue

Réponds toujours en français.

## Règles communes

Les règles communes à tous les agents sont dans [`AGENTS.md`](AGENTS.md) : dépôt public,
secrets, Git et déploiement, infrastructure réelle, documentation. Elles s'appliquent
intégralement. Ce fichier ne contient que le contexte et les consignes propres à Claude.

## Contexte général

Ce dépôt correspond à un homelab personnel basé sur Proxmox.

L'objectif est une infrastructure reproductible et propre, avec :
- Terraform pour le provisionnement des VM ;
- Ansible pour la configuration des machines ;
- Docker pour l'exécution des services applicatifs.

Documentation d'ensemble : [`docs/index.md`](docs/index.md).

---

## Principes

Deux environnements, `test` et `prod` :

1. Même code pour les deux environnements.
2. Différences uniquement via la configuration (variables).
3. Déploiement d'abord en test, puis en prod.
4. Aucune duplication inutile.
5. Chaque environnement a ses propres VM (`docker-test-01` / `docker-prod-01`,
   `haos-test-01` / `haos-prod-01`) ; aucune VM partagée.

---

## Structure en place

### Terraform

```
infra/terraform/
  modules/
    vm/          VM Ubuntu + cloud-init (hôtes Docker)
    haos/        VM Home Assistant OS
  envs/
    test/pve/docker-host/   test/pve/haos/
    prod/pve/docker-host/   prod/pve/haos/
  tf.sh          wrapper : secrets Bitwarden, template Proxmox, copie de l'état sur le NAS
```

- Les modules contiennent la logique commune ; `envs/` ne contient que la configuration.
- Un état Terraform par répertoire `envs/<env>/pve/<machine>`.

### Ansible

```
infra/ansible/
  inventories/test/   inventories/prod/   (hosts.ini, group_vars)
  playbooks/          docker-host.yml, haos.yml, nas.yml
  roles/
  deploy.sh
```

- Les playbooks sont identiques entre environnements ; les différences sont dans les
  inventaires et les `group_vars`.

### Services

- `services/docker/core/` : pile Docker générique et paramétrable (Traefik, Mosquitto,
  Zigbee2MQTT, Node-RED, Portainer).
- `services/ha/config/` : `configuration.yaml` de Home Assistant.
- `services/synology/restic-maintenance/` : maintenance Restic sur le NAS.

---

## État actuel (02/11/2026)

- Séparation test/prod en place dans Terraform, Ansible et la CI.
- Déploiement test automatique depuis `feature/**`, prod manuel depuis `main` avec
  approbation (GitHub Actions, runners auto-hébergés).
- Sauvegarde Restic de la prod et maintenance Restic sur le NAS en service.
- Réseau UniFi configuré hors IaC ; segmentation inter-VLAN non encore en place.
- Poste de travail : `dev-agent-01` (Ubuntu) ; MCP UniFi disponible en lecture seule.

---

## Rôle de Claude

Claude agit comme assistant de refactoring et d'architecture. Il doit :

- proposer des structures simples et cohérentes ;
- éviter les abstractions inutiles ;
- adapter ses propositions à un homelab, pas à une infra d'entreprise ;
- toujours privilégier simplicité, reproductibilité et clarté.

Il ne doit pas :

- introduire des outils non demandés (Kubernetes, Helm, etc.) ;
- complexifier la structure sans justification claire ;
- dupliquer des fichiers entre test et prod si une variable suffit ;
- casser la cohérence entre Terraform, Ansible et Docker.

## Attentes vis-à-vis des contributions

Quand tu proposes des changements :

- explique les choix ;
- montre des exemples concrets (arborescence, fichiers) ;
- reste minimaliste ;
- évite les patterns « entreprise » inutiles.

Si un choix est discutable, propose plusieurs options avec leurs compromis.

## MCP UniFi

Le serveur MCP `unifi` est enregistré en portée locale sur `dev-agent-01`, pas dans le
dépôt. N'utiliser que ses outils de lecture ; ne jamais créer de `.mcp.json` dans le dépôt
ni y recopier ses résultats bruts (voir [`AGENTS.md`](AGENTS.md#infrastructure-réelle)).
