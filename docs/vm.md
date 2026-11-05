# Machines virtuelles

## VM déclarées par Terraform (Git)

Valeurs par défaut des `variables.tf` de chaque environnement ; toutes sur le nœud `pve`,
stockage `local-lvm`, pont `vmbr0`, démarrage automatique.

| VM | Env | Template | vCPU | RAM | Disque | Module |
|---|---|---|---|---|---|---|
| `docker-test-01` | test | 9000 (Ubuntu) | 2 | 4 Go | 40 Go | [`modules/vm`](../infra/terraform/modules/vm/) |
| `docker-prod-01` | prod | 9000 (Ubuntu) | 4 | 8 Go | 80 Go | [`modules/vm`](../infra/terraform/modules/vm/) |
| `haos-test-01` | test | 9100 (HAOS) | 2 | 2 Go | 32 Go | [`modules/haos`](../infra/terraform/modules/haos/) |
| `haos-prod-01` | prod | 9100 (HAOS) | 2 | 4 Go | 64 Go | [`modules/haos`](../infra/terraform/modules/haos/) |

Les VM démarrent en DHCP sur le VLAN Servers ; les adresses stables passent par des
réservations DHCP dans UniFi (voir
[`HOW-TO.md`, « Trouver les IPs »](../infra/terraform/HOW-TO.md#trouver-les-ips-des-vms-après-provisioning)).
Le tableau décrit la configuration Git ; l'état réel est relevé ci-dessous.

### Correspondance avec l'état constaté (03/11/2026)

Relevé en lecture seule par le [MCP Proxmox](poste-de-travail.md#mcp-proxmox), sans
exécuter Terraform.

- **Les quatre VM existent, sont démarrées et correspondent à Git** : nom, vCPU (type
  `host`), RAM, taille et stockage du disque `scsi0`, pont `vmbr0`, démarrage automatique ;
  pour les hôtes Docker, agent QEMU actif, cloud-init en DHCP et snippet propre à la VM.
- **Écart possible** : l'agent QEMU est actif sur les deux VM Home Assistant (hérité du
  template), alors que le module `haos` ne déclare pas de bloc `agent`.
- **Non vérifié** : le résultat d'un futur `terraform plan`. Les états Terraform n'ont pas
  été consultés et Terraform n'est pas installé sur le poste ; une correspondance des
  valeurs principales ne garantit pas un plan vide.

### Hôtes Docker (`modules/vm`)

- Amorçage cloud-init : utilisateur `remi` (sudo sans mot de passe, mot de passe
  verrouillé), clés SSH publiques issues de Bitwarden, root interdit en SSH,
  authentification par mot de passe désactivée, agent QEMU, fuseau Europe/Paris.
- Configuration Ansible ([`playbooks/docker-host.yml`](../infra/ansible/playbooks/docker-host.yml)) :
  rôles `common`, `docker`, `compose_deploy`, `restic_backup`, `github_runner`.
- Données des services sous `/srv/docker/core`, propres à chaque VM.
- Chaque hôte porte aussi le runner GitHub Actions de son environnement (voir
  [CI/CD](ci-cd.md)).

### VM Home Assistant (`modules/haos`)

- Pas de cloud-init : les clés SSH sont injectées une fois par
  [`bootstrap-haos.sh`](../infra/proxmox/scripts/bootstrap-haos.sh) via l'agent QEMU.
- Accès Ansible en `root`, port SSH `22222`, sans Python (rôle `haos_config` en `raw`).
- Voir [Home Assistant](home-assistant.md).

### Poste de travail de validation `dev-agent-02` (cible)

Racine [`envs/test/pve/dev-agent-02/`](../infra/terraform/envs/test/pve/dev-agent-02/)
(Git) : 4 vCPU, 6 Go, 80 Go, UEFI/q35, template Ubuntu 24.04 dédié, DHCP. **Non créée** à
ce jour ; instance temporaire, indépendante de `dev-agent-01`, sans sauvegarde et hors de
l'inventaire Ansible. Sa création attend encore les prérequis TLS et SSH du poste
d'exécution et des vérifications Proxmox ; la confidentialité des journaux de `tf.sh` est
traitée (PR #27).
Procédure : [`DEV-AGENT.md`](../infra/terraform/DEV-AGENT.md).

## Machines hors Terraform

| Machine | Rôle | Source |
|---|---|---|
| `dev-agent-01` | Poste de travail (Ubuntu 24.04, VM KVM) ; héberge aussi code-server, cible de la route `code.reminat.com` | Constat (02/11/2026), voir [Poste de travail](poste-de-travail.md) |
| NAS Synology (`reminatnas`) | Sauvegardes, maintenance Restic | Git (inventaire `nas`) |
| Relais Wake-on-LAN (Raspberry Pi) | Réveil d'une machine à distance | Legacy, non vérifié |

La cible `code_url` (`10.10.20.10:8080`, Git) correspond à `dev-agent-01` : le
02/11/2026, cette adresse est celle du poste, et le service `code-server@remi` y écoute sur
le port 8080. Ce service n'est pas géré par le dépôt.

Ces machines sont configurées à la main (sauf la maintenance Restic du NAS, déployée par
Ansible). Leur reconstruction n'est pas encore documentée.

### VM Proxmox hors Terraform (constat du 03/11/2026)

Outre les quatre VM Terraform et les deux templates, le nœud porte trois VM créées à la
main :

| VM | État au relevé | Remarque |
|---|---|---|
| `dev-agent-01` | Démarrée | **Pas de démarrage automatique** : après un redémarrage de Proxmox, le poste de travail et ses MCP ne reviennent pas seuls |
| `vm-ubuntu-docker` | Arrêtée | Rôle non documenté (probablement un ancien essai) ; disque sur le stockage NFS du NAS |
| `hermes` | Arrêtée | Rôle non documenté |

À décider : documenter, intégrer à l'IaC ou supprimer les deux VM arrêtées (aucune action
faite).

## Cycle de vie

- Créer ou recréer : `./tf.sh <env>/pve/<machine> apply`, puis bootstrap HAOS si besoin,
  puis `./deploy.sh <env>` (voir [`PROVISIONING.md`](../infra/terraform/PROVISIONING.md)).
- Une modification de cloud-init ne s'applique qu'aux VM recréées.
- Détruire : `./tf.sh <env>/pve/<machine> destroy` ; en prod, restaurer ensuite les
  données (voir [Sauvegardes](sauvegardes.md)).
