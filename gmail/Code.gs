/* Gmail price alerts -> Telegram. Advanced Gmail service, read-only access.
 * Starts in preview mode. No message is sent until ENABLE_SEND is true.
 */

function checkFlightAlerts() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(1000)) return;
  try {
    const props = PropertiesService.getScriptProperties();
    const trips = JSON.parse(props.getProperty('TRIPS_JSON') || '[]');
    const sending = props.getProperty('ENABLE_SEND') === 'true';
    if (!trips.length) throw new Error('Configure TRIPS_JSON after inspecting a real flight alert.');
    const labels = Gmail.Users.Labels.list('me').labels || [];
    let alerts = 0;
    let checked = 0;
    const now = new Date();
    for (const trip of trips) {
      // Past journeys must not block alerts for other upcoming journeys.
      if (dateValue(trip.departure) <= dateValue(Utilities.formatDate(now, 'Europe/Athens', 'yyyy-MM-dd'))) continue;
      validateTrip(trip, now);
      const label = labels.find(item => item.name === trip.label);
      if (!label) throw new Error('Missing Gmail label: ' + trip.label);
      // Exact label IDs; no inbox-wide searches or changing read/unread state.
      const page = Gmail.Users.Messages.list('me', {
        labelIds: [label.id], q: 'newer_than:7d', maxResults: 100
      });
      // A backlog is left for manual diagnosis rather than quietly discarded.
      if (page.nextPageToken) throw new Error('More than 100 recent alerts in ' + trip.label);
      const messages = (page.messages || []).map(item =>
        Gmail.Users.Messages.get('me', item.id, {format: 'full'}));
      messages.sort((a, b) => Number(a.internalDate) - Number(b.internalDate));
      for (const message of messages) {
        checked++;
        if (props.getProperty('message_' + message.id)) continue;
        const headers = message.payload.headers || [];
        const header = name => (headers.find(h => h.name.toLowerCase() === name) || {}).value || '';
        const sender = senderAddress(header('from'));
        if (sender !== trip.sender.toLowerCase()) continue;
        const text = header('subject') + '\n' + plainText(message.payload);
        const price = extractPrice(text, trip.price_regex);
        if (price === null) {
          console.log('Unrecognized/ambiguous EUR fare in Gmail message ' + message.id);
          continue;
        }
        if (price >= 100 || price <= 0) continue;
        // Suppress subsequent emails and price changes for the same trip.
        const key = 'trip_' + trip.origin + '_' + trip.destination + '_' + trip.departure + '_' + trip.return;
        if (props.getProperty(key)) continue;
        if (!sending) {
          console.log('PREVIEW: ' + trip.origin + ' → ' + trip.destination +
                      ' ' + trip.departure + ' / ' + trip.return + ' EUR ' + price.toFixed(2));
          alerts++;
          continue;
        }
        sendTelegram(props, '✈️ Νέα προσφορά email: ' + trip.origin + ' ↔ ' + trip.destination +
          '\n' + price.toFixed(2) + ' € / άτομο με επιστροφή\n' + trip.departure +
          ' – ' + trip.return + ' · 4 διανυκτερεύσεις\n' +
          'Πηγή: ειδοποίηση email (' + trip.source + '). Η τιμή μπορεί να έχει αλλάξει.\n' +
          'Έλεγξε ώρες, ημερομηνία άφιξης, αποσκευές και τελική τιμή στην υπηρεσία πτήσεων.\n' +
          'Το όριο αφορά μόνο πτήσεις, χωρίς διαμονή.');
        props.setProperty(key, new Date().toISOString());
        props.setProperty('message_' + message.id, new Date().toISOString());
        alerts++;
      }
    }
    console.log((sending ? 'LIVE' : 'PREVIEW') + ': checked ' + checked + ', qualifying alerts ' + alerts);
  } finally {
    lock.releaseLock();
  }
}

function senderAddress(from) {
  const match = from.match(/<([^<>]+)>/);
  return (match ? match[1] : from).trim().toLowerCase();
}

function plainText(part) {
  if (part.mimeType === 'text/plain' && part.body && part.body.data) {
    return Utilities.newBlob(Utilities.base64DecodeWebSafe(part.body.data)).getDataAsString('UTF-8');
  }
  // No HTML parsing guess: require a usable plain-text email template.
  const parts = part.parts || [];
  return parts.map(plainText).filter(Boolean).join('\n');
}

function extractPrice(text, pattern) {
  // Must be a template-specific, verified regex with exactly one capture for
  // the ROUND-TRIP total in EUR per person. Do not scrape all numbers in an email.
  const matches = [...text.matchAll(new RegExp(pattern, 'gi'))];
  if (matches.length !== 1 || matches[0].length !== 2) return null;
  const value = matches[0][1].trim();
  if (!/^\d{1,3}(?:[.,]\d{2})?$/.test(value)) return null;
  const number = Number(value.replace(',', '.'));
  return Number.isFinite(number) ? number : null;
}

function dateValue(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value || '')) throw new Error('Dates must be YYYY-MM-DD.');
  const parsed = new Date(value + 'T00:00:00Z');
  if (!Number.isFinite(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== value) {
    throw new Error('Invalid calendar date.');
  }
  return parsed.getTime();
}

function validateTrip(trip, now) {
  if (trip.origin !== 'ATH' || !/^[A-Z]{3}$/.test(trip.destination || '')) {
    throw new Error('Trip must depart from ATH to a supported European destination.');
  }
  const destinations = ['VIE','BRU','SOF','ZAG','LCA','PRG','CPH','TLL','HEL','CDG','BER','BUD',
    'DUB','FCO','MXP','RIX','VNO','LUX','MLA','AMS','OSL','WAW','LIS','OTP','BTS','LJU',
    'MAD','BCN','ARN','ZRH','LHR','TIA','SJJ','TGD','SKP','BEG'];
  if (!destinations.includes(trip.destination)) throw new Error('Destination not in the European airport list.');
  const start = dateValue(trip.departure);
  const end = dateValue(trip.return);
  const today = dateValue(Utilities.formatDate(now, 'Europe/Athens', 'yyyy-MM-dd'));
  if (end - start !== 4 * 86400000 || start <= today || end > today + 180 * 86400000) {
    throw new Error('Trip must be upcoming, within 180 days, with return four days later.');
  }
  if (!trip.label || !trip.label.startsWith('cheaptravels/') || !trip.sender || !trip.source || !trip.price_regex) {
    throw new Error('Configure a dedicated per-trip label, exact sender and verified EUR fare regex.');
  }
  if (trip.verified !== true) throw new Error('Validate the email template and exact trip filter before setting verified=true.');
}

function sendTelegram(props, text) {
  const token = props.getProperty('TELEGRAM_BOT_TOKEN');
  const chat = props.getProperty('TELEGRAM_CHAT_ID');
  if (!token || !chat) throw new Error('Set Telegram credentials in Script properties.');
  // Do not print the URL or response body: they can contain credentials/chat data.
  let result;
  try {
    result = UrlFetchApp.fetch('https://api.telegram.org/bot' + token + '/sendMessage', {
      method: 'post', contentType: 'application/json',
      payload: JSON.stringify({chat_id: chat, text: text}), muteHttpExceptions: true
    });
  } catch (error) {
    throw new Error('Telegram network request failed.');
  }
  if (result.getResponseCode() !== 200 || !JSON.parse(result.getContentText()).ok) {
    throw new Error('Telegram did not confirm delivery; message was not marked sent.');
  }
}

function installTrigger() {
  // Install only after a manual preview. This function sends no Telegram message.
  const existing = ScriptApp.getProjectTriggers().filter(t => t.getHandlerFunction() === 'checkFlightAlerts');
  if (!existing.length) ScriptApp.newTrigger('checkFlightAlerts').timeBased().everyMinutes(15).create();
}

function disableAlerts() {
  PropertiesService.getScriptProperties().setProperty('ENABLE_SEND', 'false');
  ScriptApp.getProjectTriggers().filter(t => t.getHandlerFunction() === 'checkFlightAlerts')
    .forEach(t => ScriptApp.deleteTrigger(t));
}
