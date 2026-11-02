# Poste de travail

## dev-agent-01

Constat du 02/11/2026 : `dev-agent-01` est une VM (KVM) sous **Ubuntu 24.04 LTS**. C'est le
poste de travail actuel : édition du dépôt, revue, agents (Claude Code), consultation en
lecture seule du contrôleur UniFi.

Outils constatés le 02/11/2026 : `git`, `gh`, `jq`, `bws`. **Non installés** à cette date :
`terraform`, `ansible`, `bw`, `gitleaks`, `pre-commit`. Les déploiements Ansible passent
donc par la CI ; un provisionnement Terraform depuis ce poste demanderait d'installer ces
outils (versions : Terraform `>= 1.6.0`, voir les `versions.tf`).

Le poste exécute aussi **code-server** (service `code-server@remi`, port 8080), publié par
Traefik sous `code.reminat.com` (cible `code_url` dans Git). Constat du 02/11/2026 ; ce
service est installé hors dépôt.

La façon dont `dev-agent-01` est créé et sauvegardé n'est pas documentée.

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
