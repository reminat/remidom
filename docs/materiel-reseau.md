# Matériel et réseau

Cette page ne contient volontairement ni adresse MAC, ni adresse IP par équipement, ni nom
de réseau Wi-Fi, ni règle de filtrage détaillée : le dépôt est public.

## Matériel

| Équipement | Rôle | Source |
|---|---|---|
| UniFi Dream Machine SE (UniFi OS 5.1.34, Network 10.6.109) | Passerelle, routage inter-VLAN, DHCP, contrôleur UniFi, VPN | Relevé UniFi (02/11/2026) |
| Commutateurs et points d'accès UniFi | 5 commutateurs et 3 points d'accès adoptés, dont 2 commutateurs hors ligne au relevé | Relevé UniFi (02/11/2026) |
| Hôte Proxmox VE (nœud `pve`) | Virtualisation des VM | Git (Terraform), voir [Proxmox](proxmox.md) |
| NAS Synology (DSM 7) | REST Server Restic, copies de l'état Terraform, Hyper Backup | Git (inventaire `nas`), voir [Sauvegardes](sauvegardes.md) |
| Coordinateurs Zigbee SMLIGHT (réseau) | Un par environnement, joints par Zigbee2MQTT en TCP | Git (`smlight_serial_url`) |
| Raspberry Pi (relais Wake-on-LAN) | Réveil d'une machine du VLAN Servers depuis un autre réseau | Legacy, non vérifié |

Le matériel de l'hôte Proxmox et du NAS (modèle, disques) n'est pas encore documenté.

## Réseaux

Relevé UniFi du 02/11/2026. Ces données ne viennent pas de Terraform ni d'Ansible : le
réseau est configuré dans l'interface UniFi, hors IaC.

| VLAN | Nom UniFi | Sous-réseau | Usage |
|---|---|---|---|
| — (natif) | Default | 10.10.1.0/24 | Réseau principal : postes, téléphones, équipements UniFi |
| 10 | VLAN-10-MGMT | 10.10.10.0/24 | Administration |
| 20 | VLAN-20-SERVERS | 10.10.20.0/24 | Proxmox, VM, NAS |
| 30 | VLAN-30-CCTV | 10.10.30.0/24 | Vidéosurveillance |
| 40 | VLAN-40-IOT | 10.10.40.0/24 | Objets connectés |
| 80 | VLAN-80-GUEST | 10.10.80.0/24 | Invités |
| 90 | VLAN-90-LEGACY | 192.168.1.0/24 | Ancien plan d'adressage |
| — | WG-Remidom (VPN WireGuard) | 10.10.60.0/24 | Accès distant |

Le relevé montre aussi deux accès Internet (WAN), un second VPN d'accès distant UniFi et
un réseau invité historique sans VLAN, sans serveur DHCP actif.

Quatre réseaux Wi-Fi sont diffusés : un sur Default, un pour chacun des VLAN IoT, CCTV et
Invités.

### Écarts avec le plan legacy

L'ancien plan (« LAN Definition » dans le vault Obsidian) proposait cinq VLAN, dont un
VLAN 30 *Users* et un VLAN 50 *Guest*. La configuration réelle diffère :

- il n'y a pas de VLAN *Users* : les postes et téléphones sont sur **Default** ;
- le VLAN 30 sert à la **vidéosurveillance** ;
- les invités sont sur le **VLAN 80** ;
- deux réseaux s'ajoutent : **Legacy** (VLAN 90) et le **VPN WireGuard** (10.10.60.0/24).

Les plages DHCP et réservations du plan legacy ne sont pas reprises ici ; l'interface UniFi
fait foi.

## Noms DNS

Les services sont publiés sous `*.reminat.com` (prod) et `*.test.reminat.com` (test). Les
certificats sont émis par Let's Encrypt avec un challenge DNS Cloudflare (Git, voir
[Services Docker](services-docker.md#traefik)). La gestion des enregistrements DNS
(Cloudflare, DNS local) n'est pas décrite dans le dépôt.

## Segmentation

**Aucune règle de pare-feu personnalisée n'est en place** (confirmation du propriétaire,
cohérente avec le relevé UniFi du 02/11/2026). Le découpage en VLAN existe, mais la
segmentation inter-VLAN est un **chantier futur**, pas une configuration existante. Le
plan legacy de règles reste une proposition historique, non appliquée.

Seule protection versionnée liée au réseau : le middleware Traefik `lan-vpn-only`, qui
limite certaines routes HTTPS aux réseaux Default, Servers et VPN (Git,
[`roles/compose_deploy/defaults/main.yml`](../infra/ansible/roles/compose_deploy/defaults/main.yml)).
Il ne remplace pas un filtrage entre VLAN.

Quand la segmentation sera conçue, ses principes pourront être documentés ici ; le détail
des règles restera hors du dépôt public.

## Consultation du contrôleur

Un serveur MCP UniFi en lecture seule est disponible sur le poste de travail (voir
[Poste de travail](poste-de-travail.md#mcp-unifi)). Il sert aux relevés comme celui de
cette page ; il ne modifie jamais la configuration.
