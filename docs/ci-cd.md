# CI/CD

GitHub Actions, avec deux runners auto-hébergés installés par le rôle Ansible
[`github_runner`](../infra/ansible/roles/github_runner/). Terraform n'est pas exécuté par la
CI.

## Workflows (Git)

| Workflow | Fichier | Déclencheur | Effet |
|---|---|---|---|
| Secrets Scan | [`secrets-scan.yml`](../.github/workflows/secrets-scan.yml) | PR, push sur `main`, manuel | Gitleaks (runner GitHub), check `Gitleaks` |
| Gitleaks — historique complet | [`gitleaks-full.yml`](../.github/workflows/gitleaks-full.yml) | appelé par les deux workflows de déploiement | Analyse de tout l'historique ; une fuite bloque le déploiement |
| Deploy | [`deploy.yml`](../.github/workflows/deploy.yml) | push sur `feature/**` (fichiers applicatifs) ou manuel | `deploy.sh test` ou `deploy.sh prod` sur le runner de l'environnement |
| NAS — Restic maintenance | [`nas-restic-maintenance.yml`](../.github/workflows/nas-restic-maintenance.yml) | manuel uniquement | Dépose la maintenance Restic sur le NAS (`--check --diff` par défaut) ; n'exécute jamais restic |

### Règles de déclenchement de Deploy

| Événement | Résultat |
|---|---|
| Push sur `feature/**` touchant `services/docker`, `services/ha` ou `infra/ansible` (hors NAS et hors `*.md`) | déploiement **test** |
| Push sur `feature/**` ne touchant que la documentation ou le NAS | rien |
| Push sur une autre branche (`docs/**`, `prep/**`, `fix/**`…) | rien |
| Merge ou push sur `main` | rien (seul Secrets Scan tourne) |
| Lancement manuel, `test` | déploiement **test** |
| Lancement manuel depuis `main`, `prod` | déploiement **prod** après approbation de l'environnement `prod` ; refusé depuis une autre référence |

Le déploiement exclut le tag `github_runner` : un runner ne se réinstalle pas lui-même.

## Runners

| Runner | Hôte | Labels | Environnement |
|---|---|---|---|
| `docker-test-01` | `docker-test-01` | `self-hosted`, `homelab`, `test` | test |
| `docker-prod` | `docker-prod-01` | `self-hosted`, `homelab`, `prod` | prod |

Le runner tourne sous l'utilisateur `github-runner` (`/opt/github-runner`), avec Ansible et
`bws` installés par le rôle. Installation initiale et vérification :
[`HOWTO.md`, « GitHub Actions — Self-hosted runner »](../infra/ansible/HOWTO.md#github-actions--self-hosted-runner).

## Secrets et réglages GitHub

Secrets utilisés : `BWS_ACCESS_TOKEN`, `ANSIBLE_SSH_KEY`, `NAS_SSH_KEY` (voir
[Sécurité](securite.md#catalogue-des-secrets-noms-seulement)).

Les réglages du dépôt (approbation de l'environnement `prod`, ruleset sur `main`, check
`Gitleaks` obligatoire) ne sont pas versionnés. La configuration attendue est décrite dans
[`HOWTO.md`, « Réglages GitHub attendus »](../infra/ansible/HOWTO.md#réglages-github-attendus-dépôt-public) ;
l'état réel n'est pas vérifié par cette page.

## Cycle d'une modification

1. Branche dédiée depuis `main` (`feature/**` pour déclencher le déploiement test).
2. PR vers `main` : check `Gitleaks` vert obligatoire avant fusion.
3. Validation en test (automatique ou manuelle).
4. Fusion, puis lancement manuel de Deploy `prod` depuis `main`, avec approbation.
