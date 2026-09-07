# Historique des Prédictions (Prediction History)

## Description

La fonctionnalité **Historique des Prédictions** conserve automatiquement et affiche les N dernières prédictions effectuées via le dashboard ThinkTuning.

## Fonctionnalités

### 📊 Affichage de l'Historique
- **Tableau interactif** : Affiche les 50 prédictions les plus récentes
- **Colonnes** :
  - **Date/Heure** : Timestamp exact de chaque prédiction (format local)
  - **Texte** : Le texte analysé
  - **Sentiment** : Étiquette du sentiment avec couleur (positif/neutre/négatif)
  - **Confiance** : Score de confiance en pourcentage

### 🔧 Contrôles Utilisateur

#### Définir la Capacité d'Historique
```
Champ numérique : 1 à 1000 prédictions
```
- Défaut : **20 prédictions**
- Minimum : 1 prédiction
- Maximum : 1000 prédictions
- Les modifications sont **immédiatement appliquées** et persistées

#### Effacer l'Historique
- Bouton **"Effacer"** supprime toutes les prédictions
- Confirme l'action dans l'activité
- L'historique redémarre vide après l'effacement

### 💾 Persistance

Les prédictions sont **automatiquement sauvegardées** dans le **localStorage** du navigateur :
- `thinktuning.predictionsHistory` : Array de prédictions
- `thinktuning.maxHistorySize` : Taille maximale d'historique

**Conséquences** :
- ✅ L'historique persiste après rafraîchissement de page
- ✅ L'historique persiste après fermeture du navigateur
- ✅ Chaque machine/navigateur a son propre historique
- ❌ Les données locales n'affectent pas les données du serveur

### 🎯 Fonctionnement

#### Prédictions Unitaires
Quand vous cliquez sur **"Prédire"** :
1. Les prédictions sont envoyées à l'API
2. Les résultats s'affichent immédiatement
3. Les résultats sont **automatiquement ajoutés** à l'historique
4. L'historique est sauvegardé dans localStorage

#### Prédictions par Lot (CSV)
- Format **JSON** : Ajoute les prédictions à l'historique
- Format **CSV** : Télécharge le fichier (historique non modifié)

#### Gestion de la Capacité
```
Si vous avez 50 prédictions et que vous réglez la limite à 30 :
→ Les 30 les plus récentes sont conservées
→ Les 20 les plus anciennes sont supprimées
```

## Exemple d'Utilisation

### Cas d'Usage 1 : Suivre les Prédictions Récentes
```
1. Faites plusieurs prédictions via le panneau "Prédiction"
2. Allez au panneau "Historique des prédictions"
3. Consultez tous les résultats précédents avec timestamp
4. Rafraîchissez la page → Les prédictions sont toujours là
```

### Cas d'Usage 2 : Limiter la Taille de l'Historique
```
1. Vous avez 200 prédictions dans l'historique
2. Vous réglez le champ à 50
3. Seules les 50 plus récentes sont conservées
4. Les 150 anciennes sont perdues (permanent)
```

### Cas d'Usage 3 : Réinitialiser
```
1. Cliquez sur le bouton "Effacer"
2. L'historique est vidé complètement
3. Message de confirmation dans la section "Activité"
4. Prête pour une nouvelle session
```

## Détails Techniques

### Structure d'une Prédiction en Historique
```javascript
{
  text: "Ce produit est excellent",
  sentiment: "positive",
  confidence: 0.95,
  timestamp: 1724000000000  // millisecondes depuis epoch
}
```

### Stockage
- **Clés localStorage** :
  - `thinktuning.predictionsHistory`
  - `thinktuning.maxHistorySize`
- **Format** : JSON sérialisé
- **Limite** : ~5-10 MB par domaine (navigateur dépendant)

### Performance
- Affichage : 50 prédictions maximum dans le DOM (pour fluidité)
- Stock : jusqu'à 1000 prédictions en mémoire
- Scroll : Table avec overflow automatique

## FAQ

### Q: Les prédictions sont-elles stockées sur le serveur?
**R**: Non. L'historique est **local au navigateur** seulement. Le serveur ne stocke rien.

### Q: Que se passe-t-il si j'efface mes données de navigateur?
**R**: L'historique disparaîtra aussi (car il est dans localStorage).

### Q: Puis-je exporter les prédictions?
**R**: Non directement. Vous pouvez prendre une capture d'écran de la table.

### Q: Combien de prédictions puis-je conserver?
**R**: Jusqu'à 1000. Au-delà, vous risquez des problèmes de performance.

### Q: L'historique est-il partagé avec d'autres utilisateurs?
**R**: Non. Chaque navigateur/machine a son propre historique local.

### Q: Puis-je synchroniser l'historique sur plusieurs machines?
**R**: Non, ce n'est pas supporté (localStorage n'est jamais synchronisé).

## Intégration avec l'API

Aucun changement côté API n'est nécessaire. L'historique est une fonctionnalité **100% client** (navigateur).

Les endpoints `/predict` et `/predict/batch` continuent de fonctionner exactement comme avant.

## Dépannage

### L'historique ne persiste pas
- ✅ Vérifiez que localStorage est activé dans votre navigateur
- ✅ Essayez de rafraîchir la page
- ✅ Vérifiez la limite de taille du navigateur (5-10 MB)

### Les timestamps ne s'affichent pas correctement
- ✅ Vérifiez la configuration locale du navigateur
- ✅ Vérifiez le fuseau horaire du système

### Table trop lente avec beaucoup de prédictions
- ✅ Réduisez la limite max_history_size
- ✅ Cliquez "Effacer" pour redémarrer

---

**Dernière mise à jour** : 2026-08-17  
**Version** : 1.0
