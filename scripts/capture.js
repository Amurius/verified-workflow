// Capture d'une page servie sur cette machine, pour les agents de vérification.
// Blocage des hôtes non locaux et des écritures, délais et arrêt automatique : voir navigateur.js.
//
// Usage : node capture.js <url locale> <sortie.png> [sortie.html]
// Variables facultatives : CAPTURE_LARGEUR (1440), CAPTURE_HAUTEUR (900), CAPTURE_MOBILE=1 (390 x 844, tactile),
// CAPTURE_COOKIE ("nom=valeur"), CAPTURE_ECRITURE=1 (instance de dev seulement),
// CAPTURE_ATTENDRE ("sélecteur" à attendre avant la capture).
// Pour cliquer, remplir un champ ou enchaîner des écrans : scenario.js.
const fs = require('fs');
const path = require('path');
const { ouvrir, estLocale } = require('./navigateur.js');

const [urlCible, cheminCapture, cheminHtml] = process.argv.slice(2);
if (!urlCible || !cheminCapture) {
  console.log('USAGE : node capture.js <url> <sortie.png> [sortie.html]');
  process.exit(2);
}
if (!estLocale(urlCible)) {
  console.log('REFUS : seules les URL locales (localhost, 127.0.0.1) sont permises.');
  process.exit(2);
}

(async () => {
  const n = await ouvrir({ origine: urlCible });
  try {
    const reponse = await n.page.goto(urlCible, { waitUntil: 'load' });
    // Une page de dev garde souvent une connexion ouverte : on n'attend le calme du réseau que 5 s
    await n.page.waitForLoadState('networkidle', { timeout: 5000 }).catch(() => {});
    if (process.env.CAPTURE_ATTENDRE) await n.page.waitForSelector(process.env.CAPTURE_ATTENDRE);
    console.log('statut : ' + (reponse ? reponse.status() : 'aucune réponse'));
    console.log('url finale : ' + n.page.url());
    console.log('titre : ' + (await n.page.title()));
    await n.page.screenshot({ path: cheminCapture, fullPage: true });
    console.log('capture : ' + path.resolve(cheminCapture));
    if (cheminHtml) {
      fs.writeFileSync(cheminHtml, await n.page.content(), 'utf8');
      console.log('html : ' + path.resolve(cheminHtml));
    }
  } catch (erreur) {
    console.log('ERREUR : ' + erreur.message.split('\n')[0]);
    process.exitCode = 1;
  } finally {
    n.bilan();
    await n.fermer();
  }
})();
