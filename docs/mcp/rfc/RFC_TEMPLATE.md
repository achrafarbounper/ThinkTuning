# RFC MCP — {{TITLE}}

> **Statut** : `Draft` | `Review` | `Accepted` | `Rejected` | `Deprecated`
>
> **Auteur** : {{AUTHOR}}
> **Date** : {{DATE}}
> **MCP Product Council** : à valider

---

## 📋 Résumé

{{résumé de la feature, en 2 phrases}}

---

## 🎯 Motivation

Pourquoi ce tool / resource / prompt / change est nécessaire ?

- Problème actuel
- Solution proposée
- Impact client

---

## 🛡️ Sécurité

- Scope requis (`read_only` / `contributor` / `operator` / `admin`)
- Policy interne (`AUTO_APPROVE` / `APPROVE` / `REJECT`)
- Risk : `safe` / `restricted` / `dangerous`
- Audit : `ACT_MCP_*` event

---

## 📦 Breaking Change

- Oui / Non
- Si oui : migration path + deprecation policy
- Clients impactés : {{liste}}

---

## 📊 Versioning

- Version cible : `v{{x.y.0}}`
- Priorité : `High` / `Medium` / `Low`
- Roadmap liée : `docs/mcp/MCP_ROADMAP.md`

---

## ✅ Checklist

- [ ] Description claire du tool/resource/prompt
- [ ] Schéma / URI / template défini
- [ ] Scope et policy de sécurité définis
- [ ] Audit trail (event MCP)
- [ ] Version cible mise à jour
- [ ] Changelog pré-rédigé
- [ ] Migration guide (si breaking)
- [ ] Review par MCP Product Council

---

## 🏷️ Tags

`rfc`, `mcp`, `{{type:tool|resource|prompt|security|breaking}}`
