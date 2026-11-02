# Sauvegardes

Vue d'ensemble. Les procédures détaillées restent dans les guides liés.

| Données | Mécanisme | Destination | Statut |
|---|---|---|---|
| Données Docker de `docker-prod-01` (`/srv/docker/core`) | Restic, rôle `restic_backup`, quotidien à 02:30 | REST Server du NAS, repository `docker-prod` (append-only) | Git ; constat du 31/10/2026 dans le README Restic |
| Données Docker de `docker-test-01` | Restic, timer installé à la main (03:00) | REST Server du NAS, repository `docker-test` | **hors dépôt** ; constat du 31/10/2026 dans le README Restic |
| Repositories Restic | Contrôle quotidien, `forget` et `prune` planifiés sur le NAS | NAS (exécution locale) | Git (rôle `restic_maintenance`) ; constat du 31/10/2026 |
| État Terraform | Copie horodatée par `tf.sh` après `apply` ou `destroy` | NAS (SSH, `rsync`) | Git |
| Home Assistant | Sauvegardes propres à Home Assistant | Non documenté | Non vérifié |
| VM Proxmox (vzdump) | — | — | Aucune sauvegarde décrite dans le dépôt |
| NAS | Snapshots Btrfs (05:00) et Hyper Backup (06:00) | Destination Hyper Backup non documentée | Mentionné dans le README Restic ; non détaillé |
| Poste de travail `dev-agent-01` | — | — | Non documenté |

## Restic

- **Sauvegarde des hôtes Docker** (client, append-only, cohérence à chaud, restauration) :
  [`infra/ansible/HOWTO.md`, « Sauvegarde Restic des hôtes Docker »](../infra/ansible/HOWTO.md#sauvegarde-restic-des-hôtes-docker).
- **Maintenance sur le NAS** (contrôle dry-run, rétention, `forget`, `prune`, tâches DSM,
  état réel au 31/10/2026) :
  [`services/synology/restic-maintenance/README.md`](../services/synology/restic-maintenance/README.md).

La sauvegarde de `docker-test-01` n'est pas activée par Ansible
(`restic_backup_enabled` absent de l'inventaire test, donc `false`) : son timer a été créé
manuellement pendant la validation. L'aligner sur l'IaC est un point ouvert.

## État Terraform

[`tf.sh`](../infra/terraform/tf.sh) copie `terraform.tfstate` (et `terraform.tfstate.backup`
s'il existe) sur le NAS après chaque `apply` ou `destroy`, dans un dossier par
environnement et machine (`prod_pve_docker-host`…), avec un horodatage. L'état de
référence reste local au poste qui exécute Terraform : il faut le récupérer sur le NAS
avant de travailler depuis un autre poste. Aucune rétention n'est appliquée aux copies.

## Home Assistant

Non couvert par Restic. Deux scénarios de reprise figurent dans l'ancienne documentation
(Legacy, non testés) : restaurer une sauvegarde vzdump de la VM, ou recréer la VM avec
Terraform puis restaurer une sauvegarde Home Assistant. Aucun des deux n'est outillé dans
le dépôt.

## Reprise après sinistre (cible)

Scénario de référence repris de l'ancienne documentation, à valider par un exercice :

1. Réinstaller Proxmox VE et sa configuration réseau minimale (non documentée).
2. Rétablir l'accès au poste de travail, à GitHub et à Bitwarden.
3. Récupérer l'état Terraform sur le NAS, puis recréer les VM (`tf.sh`).
4. Bootstrap HAOS, puis `deploy.sh` pour configurer les hôtes.
5. Restaurer les données Docker depuis Restic et Home Assistant depuis ses sauvegardes.

Les étapes 1, 2 et la restauration de Home Assistant ne sont pas documentées.
