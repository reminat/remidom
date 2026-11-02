# AGENTS.md

Règles communes à tous les agents (Claude Code, Codex, autres) et aux contributeurs
humains. Les consignes propres à un outil vont dans son fichier (par exemple
[`CLAUDE.md`](CLAUDE.md)), sans contredire celles-ci.

## Langue

Répondre, documenter et rédiger les messages de commit et de PR en **français**.

## Dépôt public

Le dépôt est public. Ne jamais y écrire, ni dans un commit, une PR, une issue ou un
journal de CI :

- secret, mot de passe, jeton, clé privée, valeur lue dans Bitwarden (même partielle) ;
- adresse MAC, adresse IP par appareil, nom ou mot de passe de réseau Wi-Fi ;
- inventaire détaillé des clients réseau ou règles de pare-feu détaillées ;
- description d'une faiblesse exploitable.

Avant chaque commit : relire le diff et lancer Gitleaks si disponible (`pre-commit` ou
`gitleaks detect --config .gitleaks.toml`). Le check `Gitleaks` de la PR doit être vert.

## Architecture et style

- Même code pour `test` et `prod` ; différences uniquement par variables (inventaires,
  `group_vars`, variables Terraform). Aucune duplication si une variable suffit.
- Outils en place : Terraform, Ansible, Docker Compose. Ne pas introduire d'outil non
  demandé (Kubernetes, Helm…). Rester simple, lisible, adapté à un homelab.
- Écrire comme le code voisin : mêmes conventions, même densité de commentaires.
- Expliquer les choix ; proposer plusieurs options avec leurs compromis si un choix est
  discutable.

## Git et déploiement

- Travailler sur une branche dédiée depuis `main`, puis ouvrir une PR. Jamais de push
  direct ni de fusion sans revue du propriétaire ; checks verts obligatoires.
- Une branche `feature/**` qui touche des fichiers applicatifs **déclenche un déploiement
  test** : utiliser un autre préfixe (`docs/`, `prep/`, `fix/`) quand ce n'est pas voulu.
- Ne lancer aucun déploiement, workflow, `terraform apply` ou `deploy.sh` sans demande
  explicite. La prod se déploie uniquement depuis `main`, avec approbation.
- Ne pas modifier un script pour qu'il corresponde à la documentation : corriger la
  documentation, ou proposer le changement de code séparément.
- Ne pas supprimer de runs ou de branches de diagnostic (`diag/*`) avant revue.

## Infrastructure réelle

- L'état réel n'est pas connu par le dépôt. Toute action sur un système réel (Proxmox,
  VM, NAS, UniFi, GitHub) demande l'accord explicite du propriétaire.
- **UniFi** : consultation en lecture seule uniquement (MCP UniFi local, voir
  [`docs/poste-de-travail.md`](docs/poste-de-travail.md#mcp-unifi)). Aucune écriture,
  même pour tester un refus. Ne pas contourner les droits du compte de consultation.
- Ne jamais lire ni afficher la valeur d'un secret pour vérifier une configuration.

## Documentation

- Point d'entrée : [`docs/index.md`](docs/index.md). Les guides opératoires restent à côté
  du code ; `docs/` donne la vue d'ensemble et renvoie vers eux, sans les dupliquer.
- Liens Markdown **relatifs**.
- Indiquer la source de chaque information importante et distinguer : configuration Git,
  relevé UniFi daté, constat réel daté, legacy, non vérifié, cible (légende dans
  [`docs/index.md`](docs/index.md#statut-des-informations)).
- Dater les constats (JJ/MM/AAAA). Ne pas présenter la configuration Git comme l'état réel.
- L'ancien vault Obsidian est une source historique, en lecture seule ; ses procédures
  macOS sont obsolètes (poste actuel : `dev-agent-01`, Ubuntu).
- Ne pas créer d'ADR rétrospectif sans justification établie ; voir
  [`docs/decisions/index.md`](docs/decisions/index.md).
