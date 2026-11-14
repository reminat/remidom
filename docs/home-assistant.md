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
- Le modèle active `default_config`. Il ne contient plus de bloc `http:` : depuis Core
  2026.8, le proxy de confiance se règle dans l'interface (voir
  [ci-dessous](#serveur-http-et-proxy-de-confiance)).
- Avant de déployer, le rôle lit la version de Core (`ha core info`) et s'arrête si elle
  est antérieure à 2026.8 ou si le modèle contient de nouveau `http:`.

Tout le reste (intégrations, appareils, automatisations, tableaux de bord, modules
complémentaires) est configuré dans l'interface de Home Assistant et **n'est pas
versionné**.

Procédure : [`HOWTO.md`, « HAOS (Home Assistant OS) »](../infra/ansible/HOWTO.md#haos-home-assistant-os).
Premier accès SSH : [`HOW-TO.md`, « Bootstrap SSH sur les VMs HAOS »](../infra/terraform/HOW-TO.md#bootstrap-ssh-sur-les-vms-haos).

## Serveur HTTP et proxy de confiance

Traefik (sur l'hôte Docker du même environnement) transmet l'adresse du client dans
l'en-tête `X-Forwarded-For`. Home Assistant ne l'accepte que d'un proxy de confiance ;
sinon il répond **400** à toutes les requêtes passant par Traefik.

**Depuis Core 2026.8** ([documentation `http`](https://www.home-assistant.io/integrations/http/)),
ces réglages sont dans *Paramètres → Système → Réseau → Serveur HTTP* (stockage interne,
non versionné). Un bloc `http:` dans `configuration.yaml` est importé une seule fois, au
premier démarrage, puis ignoré, avec la réparation « HTTP YAML configuration is ignored
after migration ». Le dépôt a contenu ce bloc jusqu'au 14/11/2026 : c'est l'origine de
l'avertissement.

Valeurs attendues (les mêmes que l'ancien bloc YAML) :

| Champ (interface en anglais) | Valeur |
|---|---|
| *Trust X-Forwarded-For* | activé |
| *Trusted proxies* | adresse de l'hôte Docker de l'environnement (`docker_host_ip` dans `inventories/<env>/group_vars/haos.yml`), et `172.16.0.0/12` |
| Port, certificats SSL | valeurs par défaut (8123, pas de SSL : Traefik termine le HTTPS) |

`172.16.0.0/12` (réseaux Docker) vient de l'ancien modèle. Traefik joint la VM HAOS en
passant par l'adresse de l'hôte Docker : cette plage n'est probablement pas nécessaire.
La retirer est une simplification possible, à valider en test (réponse 200 via Traefik).

**Bug connu** ([core#181491](https://github.com/home-assistant/core/issues/181491), ouvert,
constaté jusqu'en 2026.10.4) : la liste *Trusted proxies* peut ne pas être enregistrée
après la migration ou une saisie. Toujours relire la valeur après enregistrement et tester
l'accès via Traefik.

### Sur un HAOS vierge (reconstruction, tour de spare)

Le réglage n'est plus déployé par Ansible : il fait partie de la reconstruction.

1. Créer la VM, faire le bootstrap SSH et l'accueil de Home Assistant, puis
   `./deploy.sh <env> --limit haos` (le rôle vérifie la version de Core).
   En cas de restauration d'une sauvegarde Home Assistant, le réglage est restauré avec
   elle : passer directement au contrôle (étape 4).
2. Se connecter en **accès direct** (`http://haos-vm.<domaine>:8123`), pas via Traefik.
3. *Paramètres → Système → Réseau → Serveur HTTP* : saisir les valeurs ci-dessus et
   enregistrer. Home Assistant redémarre son serveur HTTP et annule le changement s'il
   n'est pas confirmé dans les 5 minutes : confirmer depuis l'accès direct.
4. Contrôler : la page relue affiche les deux valeurs ; `https://haos.<domaine>` répond
   (200, certificat valide) ; *Paramètres → Système → Réparations* ne signale rien sur HTTP.

## Liens avec les autres services

- MQTT : broker Mosquitto de l'hôte Docker du même environnement (port 1883).
- Zigbee : Zigbee2MQTT publie les appareils dans MQTT ; Home Assistant les consomme par
  l'intégration MQTT. Cette intégration est configurée dans l'interface : non vérifié.

## Sauvegarde

Home Assistant n'est pas couvert par la sauvegarde Restic des hôtes Docker. Il doit
utiliser ses propres sauvegardes ; leur configuration (fréquence, destination, rétention)
n'est pas documentée ni vérifiée. Voir [Sauvegardes](sauvegardes.md#home-assistant).
