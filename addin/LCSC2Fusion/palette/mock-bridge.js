/* Mock bridge for inspecting the palette in a normal browser. NOT used inside Fusion.
 *
 * This file only defines window.LcscMock.install(deliver). app.js calls it when no real
 * `adsk.fusionSendData` shows up within a short wait. It never touches window.adsk, so nothing
 * changes when the page runs in Fusion. `deliver(action, data)` pushes a message to the page the
 * way palette.sendInfoToHTML would. Try: C2040, "resistor", "none" (no hits), "fail" (error).
 */
(function () {
  'use strict';

  const SAMPLE_PARTS = [
    {
      lcsc_id: 'C2040',
      mpn: 'RP2040',
      manufacturer: 'Raspberry Pi',
      package: 'LQFN-56',
      description: 'Dual ARM Cortex-M0+ microcontroller, 264KB SRAM, USB 1.1',
      library_type: 'Extended',
      stock: 120000,
    },
    {
      lcsc_id: 'C25804',
      mpn: '0603WAF1002T5E',
      manufacturer: 'UNI-ROYAL',
      package: '0603',
      description: '10kOhms 100mW 0603 thick film resistor, +/-1%',
      library_type: 'Basic',
      stock: 9000000,
    },
    {
      lcsc_id: 'C14663',
      mpn: 'CC0603KRX7R9BB104',
      manufacturer: 'YAGEO',
      package: '0603',
      description: '100nF 50V X7R +/-10% 0603 multilayer ceramic capacitor',
      library_type: 'Basic',
      stock: 7000000,
    },
    {
      lcsc_id: 'C2913202',
      mpn: 'ESP32-S3-WROOM-1-N16R8',
      manufacturer: 'Espressif Systems',
      package: 'SMD-41',
      description:
        'Wi-Fi and Bluetooth LE module with dual core Xtensa LX7, 16MB flash and 8MB octal PSRAM, PCB antenna, a deliberately long description to test line clamping',
      library_type: 'Extended',
      stock: 5000,
    },
  ];

  const MOCK_MANUAL_STEPS =
    'File > Open > Open from my computer, select the .lbr file, then File > Save into a project so it appears in the Library Manager.';
  const MOCK_INSTRUCTIONS =
    'Press "Open in Fusion Electronics", then use File > Save in the editor and pick a project so the library appears in the Library Manager. If that does not work, do it manually: ' +
    MOCK_MANUAL_STEPS;

  function install(deliver) {
    const later = (ms, fn) => setTimeout(fn, ms);

    function search(query) {
      deliver('busy', { value: true });
      deliver('status', { level: 'info', text: 'Searching for "' + query + '"...' });
      later(600, () => {
        const q = query.toLowerCase();
        if (q === 'fail') {
          deliver('error', { message: 'Mock: the search service is not reachable.' });
          deliver('busy', { value: false });
          return;
        }
        let items;
        if (/^c\d+$/.test(q)) {
          items = SAMPLE_PARTS.filter((part) => part.lcsc_id.toLowerCase() === q);
        } else if (q === 'none') {
          items = [];
        } else if (q === 'resistor' || q === 'all') {
          items = SAMPLE_PARTS;
        } else {
          items = SAMPLE_PARTS.filter((part) =>
            (part.mpn + ' ' + part.description).toLowerCase().includes(q)
          );
        }
        deliver('results', { query: query, items: items });
        deliver('status', items.length
          ? { level: 'ok', text: 'Found ' + items.length + ' part(s) for "' + query + '".' }
          : { level: 'warn', text: 'No parts found for "' + query + '".' });
        deliver('busy', { value: false });
      });
    }

    function add(lcscId) {
      const part = SAMPLE_PARTS.find((candidate) => candidate.lcsc_id === lcscId);
      deliver('busy', { value: true });
      deliver('status', { level: 'info', text: 'Downloading ' + lcscId + ' and building the library...' });
      later(900, () => {
        const withWarnings = lcscId === 'C2913202';
        deliver('added', {
          lcsc_id: lcscId,
          path: 'C:\\Users\\mock\\LCSC2Fusion\\libraries\\' + lcscId + '.lbr',
          mpn: part ? part.mpn : '',
          warnings: withWarnings ? [
            lcscId + ': pad 41 is shared by 9 pads, renamed to 41, 41_2, ...',
            lcscId + ': polygon pad approximated by its bounding box',
          ] : [],
          opened: false,
          instructions: MOCK_INSTRUCTIONS,
        });
        deliver('status', withWarnings
          ? { level: 'warn', text: 'Saved ' + lcscId + ' with 2 warning(s).' }
          : { level: 'ok', text: 'Saved ' + lcscId + '.' });
        deliver('busy', { value: false });
      });
    }

    // Like Fusion: opening works for every library except C2913202, which shows the fallback.
    function openLibrary(path) {
      const name = String(path).split(/[\\/]/).pop();
      if (name === 'C2913202.lbr') {
        deliver('status', { level: 'warn', text: 'Fusion did not open ' + name + ' (mock failure). Open it manually: ' + MOCK_MANUAL_STEPS });
      } else {
        deliver('status', { level: 'ok', text: 'Sent ' + name + ' to Fusion for opening. In the editor use File > Save and pick a project so it appears in the Library Manager.' });
      }
    }

    return {
      fusionSendData: function (action, dataString) {
        const data = dataString ? JSON.parse(dataString) : {};
        switch (action) {
          case 'ready':
            deliver('busy', { value: false });
            deliver('status', { level: 'info', text: 'Ready (mock). Libraries are saved to C:\\Users\\mock\\LCSC2Fusion\\libraries' });
            break;
          case 'search':
            search(data.query);
            break;
          case 'add':
            add(data.lcsc_id);
            break;
          case 'open_library':
            openLibrary(data.path);
            break;
          case 'open_folder':
            deliver('status', { level: 'info', text: 'Mock: would open ' + data.path });
            break;
          default:
            deliver('error', { message: 'Mock: unknown action ' + action });
        }
        return Promise.resolve('OK');
      },
    };
  }

  window.LcscMock = { install: install };
})();
