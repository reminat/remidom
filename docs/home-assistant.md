# Home Assistant

## Déploiement (Git)

| | test | prod |
|---|---|---|
| VM | `haos-test-01` | `haos-prod-01` |
| Accès SSH (Ansible) | `haos-vm.test.reminat.com:22222`, `root` | `haos-vm.reminat.com:22222`, `root` |
| Accès web direct | `http://haos-vm.test.reminat.com:8123` | `http://haos-vm.reminat.com:8123` |
| Accès via Traefik | `https://haos.test.reminat.com` | `https://haos.reminat.com` |

Home Assistant OS tourne dans une VM dédiée créée par le module Terraform
[`modules/haos`](../infra/terraform/modules/haos/), à partir du template 9100 (voir
[Proxmox](proxmox.md#templates)). Le port SSH 22222 suppose un accès SSH au système HAOS
configuré au bootstrap ; l'état réel des modules complémentaires n'est pas vérifié.

## Ce que gère Ansible

Seul `configuration.yaml` est versionné :
[`services/ha/config/configuration.yaml.j2`](../services/ha/config/configuration.yaml.j2).

- Le rôle [`haos_config`](../infra/ansible/roles/haos_config/) rend le modèle, le compare
  au fichier en place et ne le remplace que s'il diffère, puis redémarre le cœur de Home
  Assistant (`ha core restart`).
- HAOS n'a pas Python : le rôle n'utilise que le module `raw`.
- Le modèle active `default_config` et autorise Traefik comme proxy de confiance
  (adresse de l'hôte Docker de l'environnement et réseaux Docker).

Tout le reste (intégrations, appareils, automatisations, tableaux de bord, modules
complémentaires) est configuré dans l'interface de Home Assistant et **n'est pas
versionné**.

Procédure : [`HOWTO.md`, « HAOS (Home Assistant OS) »](../infra/ansible/HOWTO.md#haos-home-assistant-os).
Premier accès SSH : [`HOW-TO.md`, « Bootstrap SSH sur les VMs HAOS »](../infra/terraform/HOW-TO.md#bootstrap-ssh-sur-les-vms-haos).

## Liens avec les autres services

- MQTT : broker Mosquitto de l'hôte Docker du même environnement (port 1883).
- Zigbee : Zigbee2MQTT publie les appareils dans MQTT ; Home Assistant les consomme par
  l'intégration MQTT. Cette intégration est configurée dans l'interface : non vérifié.

## Sauvegarde

Home Assistant n'est pas couvert par la sauvegarde Restic des hôtes Docker. Il doit
utiliser ses propres sauvegardes ; leur configuration (fréquence, destination, rétention)
n'est pas documentée ni vérifiée. Voir [Sauvegardes](sauvegardes.md#home-assistant).
