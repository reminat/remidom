# Index des décisions techniques

Cet index recense les décisions **traçables** : une PR, un commentaire de configuration ou
un guide en donne la justification. Il ne crée pas d'ADR rétrospectif : une décision dont
la justification n'est pas établie figure dans la dernière section, à formaliser plus tard.

Les nouvelles décisions importantes pourront être rédigées dans ce dossier
(`docs/decisions/NNNN-titre.md` : contexte, décision, conséquences), puis ajoutées ici.

## Décisions établies

| Sujet | Décision | Source |
|---|---|---|
| Environnements | Même code pour test et prod, différences par variables ; VM distinctes | [`CLAUDE.md`](../../CLAUDE.md) |
| Outillage | Terraform, Ansible, Docker Compose ; pas de Kubernetes ni d'outil lourd | [`CLAUDE.md`](../../CLAUDE.md) |
| Secrets | Bitwarden Secrets Manager, injection à l'exécution, jamais dans le dépôt | [`tf.sh`](../../infra/terraform/tf.sh), [`deploy.sh`](../../infra/ansible/deploy.sh) |
| VM jetables | cloud-init au premier démarrage seulement ; modification = recréation de la VM | [`infra/proxmox/README.md`](../../infra/proxmox/README.md) |
| Déploiement prod | Lancement manuel depuis `main`, approbation obligatoire, jamais au merge | PR #18, [`deploy.yml`](../../.github/workflows/deploy.yml) |
| Runners | Un runner par environnement, chaque job sur le runner de son environnement | PR #8 |
| Scan de secrets | Gitleaks sur chaque PR et sur tout l'historique avant chaque déploiement | PR #18, [`gitleaks-full.yml`](../../.github/workflows/gitleaks-full.yml) |
| Accès Proxmox via Traefik | Réservé LAN et VPN, CA Proxmox vérifiée | PR #9 |
| Sauvegarde des hôtes Docker | Restic vers REST Server append-only ; sauvegarde à chaud, Portainer seul arrêté | PR #13, [`HOWTO.md`](../../infra/ansible/HOWTO.md#cohérence-à-chaud-service-par-service) |
| Maintenance Restic | Exécutée localement sur le NAS ; contrôle dry-run, `forget`/`prune` séparés et désactivés par défaut | PR #10, #17, [README Restic](../../services/synology/restic-maintenance/README.md) |
| Opérations réelles Restic | Validées sur test avant activation en prod | PR #19, #20, #21 |
| MCP UniFi | Consultation en lecture seule, compte *View Only*, configuration hors dépôt | [Poste de travail](../poste-de-travail.md#mcp-unifi) |

## À formaliser (justification non établie)

- Découpage Terraform `envs/<env>/pve/<machine>` avec un état par machine, plutôt qu'un
  état par environnement.
- État Terraform local avec copie sur le NAS, plutôt qu'un backend distant.
- Réseau configuré dans UniFi, hors IaC.
- Politique de rétention Restic (marquée « à valider » dans le README Restic).
- Stratégie de sauvegarde de Home Assistant et des VM Proxmox.
- Segmentation inter-VLAN (chantier futur).
