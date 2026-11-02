# Documentation remidom

Point d'entrée de la documentation du homelab. Les guides opératoires détaillés restent à
côté du code qu'ils décrivent ; les pages de `docs/` donnent la vue d'ensemble et renvoient
vers eux.

## Statut des informations

Chaque information importante indique sa source. Par ordre de fiabilité :

| Marqueur | Signification |
|---|---|
| **Git** | Configuration versionnée dans ce dépôt (branche `main`) : ce qui *doit* être déployé. |
| **Relevé UniFi (date)** | Configuration réseau lue sur le contrôleur UniFi à la date indiquée, en lecture seule. |
| **Constat (date)** | État réel observé à la date indiquée (commande, interface, journal). |
| **Legacy** | Ancienne documentation Obsidian : historique, à confronter avant tout usage. |
| **Non vérifié** | Information plausible, sans contrôle réel récent. |
| **Cible** | État visé, pas encore en place. |

La configuration Git ne prouve pas l'état réel : une VM ou un réglage peut avoir divergé
depuis le dernier déploiement.

## Vue d'ensemble

- [Architecture générale](architecture.md) : rôles de Terraform, Ansible et Docker,
  environnements `test` et `prod`, flux de déploiement.
- [Matériel et réseau](materiel-reseau.md) : équipements, VLAN, VPN, segmentation.
- [Proxmox](proxmox.md) : hôte de virtualisation, templates, snippets cloud-init.
- [Machines virtuelles](vm.md) : VM déclarées par Terraform et machines hors Terraform.
- [Services Docker](services-docker.md) : pile `core` (Traefik, Mosquitto, Zigbee2MQTT,
  Node-RED, Portainer).
- [Home Assistant](home-assistant.md) : VM HAOS et configuration gérée par Ansible.
- [Sécurité](securite.md) : secrets, accès, analyse Gitleaks, points ouverts.
- [Sauvegardes](sauvegardes.md) : Restic, état Terraform, Home Assistant, Proxmox.
- [CI/CD](ci-cd.md) : workflows GitHub Actions, runners, approbations.
- [Poste de travail](poste-de-travail.md) : dev-agent-01, outils, MCP UniFi.

## Procédures et décisions

- [Index des procédures](procedures/index.md)
- [Index des décisions techniques](decisions/index.md)

## Guides à proximité du code

| Guide | Sujet |
|---|---|
| [`infra/terraform/HOW-TO.md`](../infra/terraform/HOW-TO.md) | Provisionnement complet : prérequis, secrets, templates, IP, bootstrap HAOS |
| [`infra/terraform/PROVISIONING.md`](../infra/terraform/PROVISIONING.md) | Aide-mémoire des commandes de provisionnement |
| [`infra/proxmox/README.md`](../infra/proxmox/README.md) | Principes Terraform, cloud-init et snippets Proxmox |
| [`infra/ansible/HOWTO.md`](../infra/ansible/HOWTO.md) | Déploiement Ansible, secrets, runners, Traefik, sauvegarde Restic des hôtes Docker |
| [`services/synology/restic-maintenance/README.md`](../services/synology/restic-maintenance/README.md) | Maintenance Restic sur le NAS : contrôle, `forget`, `prune`, état réel |

## Règles pour les contributeurs

Les règles communes (humains et agents) sont dans [`AGENTS.md`](../AGENTS.md). En
particulier : dépôt **public**, aucun secret ni inventaire réseau détaillé, et pas de
duplication entre `docs/` et les guides ci-dessus.
