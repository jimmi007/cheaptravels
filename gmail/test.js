const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');

function setup(sending = false) {
  const day = new Date().toISOString().slice(0, 10);
  const departure = new Date(day + 'T00:00:00Z');
  departure.setUTCDate(departure.getUTCDate() + 10);
  const returning = new Date(departure);
  returning.setUTCDate(returning.getUTCDate() + 4);
  const trip = {origin: 'ATH', destination: 'VIE', departure: departure.toISOString().slice(0, 10),
    return: returning.toISOString().slice(0, 10), label: 'cheaptravels/test',
    sender: 'alerts@example.com', source: 'Fixture', verified: true,
    price_regex: 'Round-trip fare: ([0-9]+(?:[.,][0-9]{2})?) EUR'};
  const properties = new Map(Object.entries({TRIPS_JSON: JSON.stringify([trip]),
    ENABLE_SEND: sending ? 'true' : 'false', TELEGRAM_BOT_TOKEN: 'test-only', TELEGRAM_CHAT_ID: 'test-only'}));
  let text = 'Round-trip fare: 99.99 EUR';
  let delivered = 0;
  let fail = false;
  const logs = [];
  const context = vm.createContext({
    console: {log: value => logs.push(value)},
    LockService: {getScriptLock: () => ({tryLock: () => true, releaseLock: () => {}})},
    PropertiesService: {getScriptProperties: () => ({
      getProperty: key => properties.get(key) || null,
      setProperty: (key, value) => properties.set(key, value)})},
    Utilities: {
      formatDate: date => date.toISOString().slice(0, 10),
      base64DecodeWebSafe: value => Buffer.from(value, 'base64url'),
      newBlob: value => ({getDataAsString: () => value.toString('utf8')})},
    Gmail: {Users: {Labels: {list: () => ({labels: [{name: trip.label, id: 'label-1'}]})},
      Messages: {
        list: (user, params) => {
          assert.equal(user, 'me');
          assert.equal(params.labelIds[0], 'label-1');
          return {messages: [{id: 'mail-1'}]};
        },
        get: () => ({id: 'mail-1', internalDate: Date.now().toString(), payload: {
          mimeType: 'text/plain', headers: [{name: 'From', value: 'Alerts <alerts@example.com>'}],
          body: {data: Buffer.from(text).toString('base64url')}}})
      }}},
    UrlFetchApp: {fetch: () => {
      if (fail) throw new Error('URL containing token must never be surfaced');
      delivered++;
      return {getResponseCode: () => 200, getContentText: () => '{"ok":true}'};
    }}
  });
  vm.runInContext(fs.readFileSync(__dirname + '/Code.gs', 'utf8'), context);
  return {context, trip, properties, logs, delivered: () => delivered,
    setText: value => {text = value;}, setFail: value => {fail = value;}};
}

test('accept only one template-matching EUR fare', () => {
  const {context, trip} = setup();
  assert.equal(context.extractPrice('Round-trip fare: 99,50 EUR', trip.price_regex), 99.5);
  assert.equal(context.extractPrice('Round-trip fare: 90 EUR\nRound-trip fare: 80 EUR', trip.price_regex), null);
  assert.equal(context.extractPrice('Round-trip fare: 90 USD', trip.price_regex), null);
  assert.equal(context.extractPrice('Hotel 40 EUR. Flight savings 90 EUR.', trip.price_regex), null);
});

test('reject unverified templates, domestic trips and incorrect durations', () => {
  const {context, trip} = setup();
  assert.throws(() => context.validateTrip({...trip, verified: false}, new Date()), /Validate/);
  assert.throws(() => context.validateTrip({...trip, destination: 'SKG'}, new Date()), /European/);
  assert.throws(() => context.validateTrip({...trip, return: trip.departure}, new Date()), /four days/);
});

test('preview reads only dedicated label and sends nothing', () => {
  const state = setup();
  state.context.checkFlightAlerts();
  assert.equal(state.delivered(), 0);
  assert.equal([...state.properties.keys()].filter(key => key.startsWith('trip_')).length, 0);
  assert.ok(state.logs.some(line => line.startsWith('PREVIEW:')));
});

test('send once, suppress repeated email and subsequent price changes', () => {
  const state = setup(true);
  state.context.checkFlightAlerts();
  state.context.checkFlightAlerts();
  state.properties.delete('message_mail-1');
  state.setText('Round-trip fare: 80 EUR');
  state.context.checkFlightAlerts();
  assert.equal(state.delivered(), 1);
});

test('100 EUR is excluded', () => {
  const state = setup(true);
  state.setText('Round-trip fare: 100 EUR');
  state.context.checkFlightAlerts();
  assert.equal(state.delivered(), 0);
});

test('failed delivery is retried without revealing token URL', () => {
  const state = setup(true);
  state.setFail(true);
  assert.throws(() => state.context.checkFlightAlerts(), /^Error: Telegram network request failed\.$/);
  assert.equal(state.properties.get('message_mail-1'), undefined);
  state.setFail(false);
  state.context.checkFlightAlerts();
  assert.equal(state.delivered(), 1);
});
