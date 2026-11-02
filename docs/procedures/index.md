# Index des procédures

Les procédures sont rédigées à côté du code concerné. Cet index renvoie vers elles ; il
n'en duplique pas le contenu.

## Provisionnement

| Procédure | Guide |
|---|---|
| Prérequis, secrets Bitwarden, jeton API Proxmox | [`HOW-TO.md`, « Prérequis » et « Secrets dans Bitwarden »](../../infra/terraform/HOW-TO.md#prérequis) |
| Créer les VM (test puis prod) | [`PROVISIONING.md`](../../infra/terraform/PROVISIONING.md) |
| Templates Proxmox (création automatique ou manuelle) | [`HOW-TO.md`, « Templates Proxmox »](../../infra/terraform/HOW-TO.md#templates-proxmox) |
| Trouver les adresses IP des VM | [`HOW-TO.md`, « Trouver les IPs »](../../infra/terraform/HOW-TO.md#trouver-les-ips-des-vms-après-provisioning) |
| Bootstrap SSH des VM HAOS | [`HOW-TO.md`, « Bootstrap SSH »](../../infra/terraform/HOW-TO.md#bootstrap-ssh-sur-les-vms-haos) |
| Détruire et recréer un environnement | [`HOW-TO.md`, « Détruire un env »](../../infra/terraform/HOW-TO.md#détruire-un-env-ex-reconstruire-test-from-scratch) |
| Modifier cloud-init (VM recréée) | [`infra/proxmox/README.md`](../../infra/proxmox/README.md#workflow-standard-à-mémoriser) |

## Configuration et déploiement

| Procédure | Guide |
|---|---|
| Déployer avec Ansible (`deploy.sh`) | [`HOWTO.md`, « Déploiement »](../../infra/ansible/HOWTO.md#déploiement) |
| Déployer la configuration Home Assistant | [`HOWTO.md`, « Playbook haos.yml »](../../infra/ansible/HOWTO.md#playbook-haosyml) |
| Créer le jeton API Cloudflare | [`HOWTO.md`, « Token API Cloudflare »](../../infra/ansible/HOWTO.md#token-api-cloudflare) |
| Installer un runner GitHub | [`HOWTO.md`, « Prérequis (une seule fois) »](../../infra/ansible/HOWTO.md#prérequis-une-seule-fois) |
| Déployer en prod depuis GitHub | [CI/CD](../ci-cd.md#cycle-dune-modification) |

## Sauvegarde et restauration

| Procédure | Guide |
|---|---|
| Créer l'utilisateur REST d'un hôte | [`HOWTO.md`, « Utilisateur REST »](../../infra/ansible/HOWTO.md#utilisateur-rest-nas-une-fois) |
| Première mise en service de la sauvegarde | [`HOWTO.md`, « Première mise en service »](../../infra/ansible/HOWTO.md#première-mise-en-service) |
| Restaurer les données Docker | [`HOWTO.md`, « Restauration »](../../infra/ansible/HOWTO.md#restauration) |
| Préparer le NAS pour la maintenance Restic | [README Restic, « Préparation du NAS »](../../services/synology/restic-maintenance/README.md#préparation-du-nas-une-fois-par-un-administrateur) |
| Déployer la maintenance Restic | [README Restic, « Déploiement »](../../services/synology/restic-maintenance/README.md#déploiement-gitops-manuel) |
| Mettre à jour les empreintes des tâches DSM | [README Restic](../../services/synology/restic-maintenance/README.md#mise-à-jour-des-empreintes-dans-les-tâches-dsm) |
| Protéger un snapshot | [README Restic, « Protéger un snapshot »](../../services/synology/restic-maintenance/README.md#protéger-un-snapshot-keep_tags) |

## Procédures manquantes

À rédiger quand le besoin se présente, après validation sur le système réel :

- installation et configuration réseau de l'hôte Proxmox ;
- sauvegarde et restauration de Home Assistant ;
- reprise complète après perte de l'hôte Proxmox (exercice à mener, voir
  [Sauvegardes](../sauvegardes.md#reprise-après-sinistre-cible)) ;
- installation du poste de travail Ubuntu ;
- relais Wake-on-LAN (procédure legacy à revalider avant reprise).
