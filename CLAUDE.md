# CLAUDE.md

## Langue

Réponds toujours en français.

## Contexte général

Ce repository correspond à un homelab personnel basé sur Proxmox.

L’objectif est de construire une infrastructure reproductible et propre en utilisant :
- Terraform pour le provisioning des VM
- Ansible pour la configuration des machines
- Docker pour l’exécution des services applicatifs

L’infrastructure actuelle permet déjà :
- de provisionner des VM via Terraform (docker host, home assistant)
- de configurer ces VM via Ansible (packages, services, docker, etc.)

---

## Objectif cible

Mettre en place une séparation claire entre deux environnements :

- `test`
- `prod`

Avec les principes suivants :

1. Même code pour les deux environnements
2. Différences uniquement via la configuration (variables)
3. Possibilité de déployer d’abord en test, puis en prod
4. Aucune duplication inutile

---

## Architecture cible (haut niveau)

### Terraform

Structure attendue :

infra/terraform/
modules/
vm/
(autres modules réutilisables)
envs/
test/
proxmox/
prod/
proxmox/

- Les modules contiennent la logique commune
- Les dossiers `envs/` contiennent uniquement la configuration spécifique (variables, tfvars)
- Chaque environnement a son propre state Terraform

---

### Ansible

Structure attendue :

infra/ansible/
inventories/
test/
prod/
playbooks/
roles/

- Les playbooks sont identiques entre environnements
- Les différences sont portées par les inventories et les group_vars
- Aucun playbook spécifique à un environnement

---

### Machines

Chaque environnement doit avoir ses propres VM :

- docker-test / docker-prod
- ha-test / ha-prod

Aucune VM partagée entre test et prod.

---

### Services Docker

Les services sont définis dans `services/docker/`.

Ils doivent être :
- génériques
- paramétrables via variables
- indépendants de l’environnement

Les différences (ports, tags, activation) doivent être gérées via Ansible ou variables.

---

## Contraintes importantes

- Pas de sur-ingénierie (pas de Kubernetes, pas de complexité inutile)
- Priorité à la lisibilité et à la maintenabilité
- Pas de duplication massive entre test et prod
- Les secrets ne doivent pas être stockés en clair dans le repo
- Les états Terraform doivent être séparés par environnement

---

## Ce que Claude doit faire

Claude agit comme un assistant de refactoring et d’architecture.

Il doit :

- proposer des structures simples et cohérentes
- éviter les abstractions inutiles
- adapter ses propositions à un homelab (pas à une infra entreprise complexe)
- toujours privilégier :
  - simplicité
  - reproductibilité
  - clarté

---

## Ce que Claude ne doit pas faire

- Introduire des outils non demandés (Kubernetes, Helm, etc.)
- Complexifier la structure sans justification claire
- Dupliquer des fichiers entre test et prod si une variable suffit
- Casser la cohérence entre Terraform, Ansible et Docker

---

## Workflow attendu

1. Déploiement en `test`
2. Validation
3. Déploiement en `prod`

Commandes typiques attendues (exemple) :

./deploy.sh test
./deploy.sh prod

---

## État actuel (à adapter)

- Terraform déjà en place pour créer des VM
- Ansible déjà utilisé pour configurer les machines
- Un seul environnement réellement utilisé (prod implicite)
- Pas encore de vraie séparation test/prod

---

## Attentes vis-à-vis des contributions

Quand tu proposes des changements :

- explique les choix
- montre des exemples concrets (arborescence, fichiers)
- reste minimaliste
- évite les patterns “entreprise” inutiles

Si un choix est discutable, propose plusieurs options avec leurs trade-offs.

