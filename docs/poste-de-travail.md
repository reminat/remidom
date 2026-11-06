# Poste de travail

## dev-agent-01

Constat du 02/11/2026 : `dev-agent-01` est une VM (KVM) sous **Ubuntu 24.04 LTS**. C'est le
poste de travail actuel : édition du dépôt, revue, agents (Claude Code), consultation en
lecture seule du contrôleur UniFi et de Proxmox VE.

Outils constatés le 02/11/2026 : `git`, `gh`, `jq`, `bws`, chaîne Rust (`cargo`). **Non
installés** à cette date : `terraform`, `ansible`, `bw`, `gitleaks`, `pre-commit`. Les déploiements Ansible passent
donc par la CI ; un provisionnement Terraform depuis ce poste demanderait d'installer ces
outils (versions : Terraform `>= 1.6.0`, voir les `versions.tf`).

Le poste exécute aussi **code-server** (service `code-server@remi`, port 8080), publié par
Traefik sous `code.reminat.com` (cible `code_url` dans Git). Constat du 02/11/2026 ; ce
service est installé hors dépôt.

La façon dont `dev-agent-01` est créé et sauvegardé n'est pas documentée. Sa
reconstruction est préparée en commençant par une VM de validation, `dev-agent-02`, créée
par Terraform à partir d'un template Ubuntu 24.04 (cible, non créée) :
[`DEV-AGENT.md`](../infra/terraform/DEV-AGENT.md).

## Procédures macOS (historique)

L'ancienne documentation et certains passages des guides (installation par `brew`,
trousseau macOS, chemins `/Users/...`, `/opt/homebrew`) concernent un ancien poste macOS.
Ils sont **historiques** : sur Ubuntu, installer les outils depuis leurs sources
officielles :

- Terraform : <https://developer.hashicorp.com/terraform/install>
- Ansible : <https://docs.ansible.com/ansible/latest/installation_guide/index.html>
- Bitwarden CLI (`bw`) : <https://bitwarden.com/help/cli/>
- Bitwarden Secrets Manager CLI (`bws`) : <https://bitwarden.com/help/secrets-manager-cli/>
- Gitleaks et pre-commit : <https://github.com/gitleaks/gitleaks>, <https://pre-commit.com/>

Le fichier [`.vscode/settings.json`](../.vscode/settings.json) pointe encore vers un
interpréteur Python macOS.

## MCP UniFi

Un serveur MCP UniFi (projet open source `unifi-network-mcp`, version épinglée) est
enregistré dans Claude Code sur `dev-agent-01`, **en portée locale** : il n'est pas déclaré
dans le dépôt (aucun `.mcp.json`).

- **Lecture seule** : mode lecture seule du serveur activé, et compte de service UniFi au
  rôle *View Only*. Aucune modification du contrôleur n'est faite par ce biais, ni même
  tentée en test.
- **Périmètre** : application UniFi Network seulement, site par défaut, sans WebSocket.
- **TLS** : vérification du certificat du contrôleur active, confiance limitée à ce
  certificat.
- **Secrets** : le mot de passe du compte de service est lu à la demande dans Bitwarden
  Secrets Manager, avec un jeton d'accès dédié, distinct des jetons de déploiement.
- **Configuration** : hors dépôt, sur le poste uniquement. Identifiants, jeton et
  emplacement des fichiers sensibles ne sont pas documentés ici.

Usage : relevés de configuration réseau (voir [Matériel et réseau](materiel-reseau.md)).
Les résultats bruts (adresses MAC, adresses IP par appareil, noms de réseaux Wi-Fi,
clients) ne doivent jamais être copiés dans le dépôt ; seules des informations agrégées
peuvent l'être.

Limites constatées : l'outil ne lit pas toutes les catégories de règles de l'interface
UniFi ; un résultat vide ne suffit pas à conclure sur le filtrage réseau.

Le certificat du contrôleur et le jeton d'accès ont des échéances de renouvellement,
suivies hors dépôt.

## MCP Proxmox

Un serveur MCP Proxmox VE (projet open source [`3kirt/proxmox-mcp`](https://github.com/3kirt/proxmox-mcp),
version 0.12.0) est enregistré dans Claude Code sur `dev-agent-01`, **en portée locale**,
sous le nom `proxmox`. Il n'est pas déclaré dans le dépôt (aucun `.mcp.json`). Installé et
validé le 03/11/2026.

- **Lecture seule par construction** : le client HTTP du projet n'émet que des requêtes GET
  (code relu à la révision installée), et aucun outil n'exécute de commande dans les
  invités.
- **Compte dédié** : jeton d'API propre au MCP, avec séparation des privilèges, rôle
  `PVEAuditor` sur `/` (privilèges `*.Audit` uniquement, vérifiés par `/access/permissions`).
  Le jeton de Terraform n'est pas utilisé.
- **Installation** : compilé avec `cargo install --locked` depuis le commit du tag `v0.12.0`,
  dans l'environnement de l'utilisateur ; aucun service ni port d'écoute.
- **TLS** : vérification active. Le nœud est joint sous le nom `pve` (présent dans le
  certificat, résolu par `/etc/hosts` sur le poste) et la seule autorité approuvée est la CA
  du cluster, celle versionnée dans
  [`roles/compose_deploy/files/pve-root-ca.pem`](../infra/ansible/roles/compose_deploy/files/pve-root-ca.pem).
  Le nom `pve.reminat.com` n'est pas dans le certificat du nœud et est refusé.
- **Secrets** : la valeur du jeton est lue au démarrage dans Bitwarden Secrets Manager, dans
  un projet dédié et avec un jeton d'accès propre à ce MCP, distinct de ceux de déploiement
  et du MCP UniFi. Aucun secret dans la configuration de Claude Code ni en argument de
  commande.
- **Configuration** : hors dépôt, sur le poste uniquement. Identifiants, jeton et
  emplacement des fichiers sensibles ne sont pas documentés ici.

Usage : état du nœud, VM et templates, stockages, réseau de l'hôte, snapshots, sauvegardes
vzdump, comparaison avec Terraform (voir [Proxmox](proxmox.md) et
[Machines virtuelles](vm.md)). Les écarts sont signalés, jamais corrigés par ce biais. Les
résultats bruts (configurations de VM, adresses MAC et IP, journaux de tâches) ne doivent
jamais être copiés dans le dépôt.

Limites : pas d'outil de détail pour les conteneurs LXC (seulement la liste des
ressources), ni pour le SDN, le pare-feu ou les ACL ; un résultat absent n'est pas une
preuve. Projet récent maintenu par une seule personne : relire le code avant toute montée
de version.

Le certificat du nœud, le jeton d'API et le jeton d'accès Bitwarden ont des échéances de
renouvellement, suivies hors dépôt.
