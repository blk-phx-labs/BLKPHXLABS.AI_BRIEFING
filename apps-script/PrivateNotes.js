// ============================================================
// 10. PRIVATE NOTES WEBHOOK
// The Cloud Function posts the founder-only notes here after each run; they're
// prepended to one running Google Doc so the newest morning is always on top.
// ============================================================

// SHA-256 of the shared secret. The secret itself lives only in Secret Manager
// (briefing-webhook-secret); a hash can't be turned back into it, so it's safe in a public repo.
const WEBHOOK_SECRET_SHA256 = '7b19a02c6a61ee57441c15d6d51b53624065a9a1bb09619ac9e4fc134b386bcf';
const NOTES_DOC_TITLE       = 'LabMind Private Notes';

function doPost(e) {
  let body;
  try {
    body = JSON.parse(e.postData.contents);
  } catch (err) {
    return jsonOut({ ok: false, error: 'bad request' });
  }
  if (!body || sha256Hex(String(body.secret || '')) !== WEBHOOK_SECRET_SHA256) {
    return jsonOut({ ok: false, error: 'unauthorized' });
  }
  if (body.type !== 'private_notes' || !body.notes) {
    return jsonOut({ ok: false, error: 'nothing to do' });
  }
  const doc = prependPrivateNotes(body);
  return jsonOut({ ok: true, doc: doc.getUrl() });
}

function prependPrivateNotes(entry) {
  const doc  = openNotesDoc();
  const body = doc.getBody();
  let i = 0;

  body.insertParagraph(i++, `${entry.date}: ${entry.lead || 'LabMind'}`)
      .setHeading(DocumentApp.ParagraphHeading.HEADING2);
  String(entry.notes).split('\n').map(l => l.trim()).filter(Boolean).forEach(line => {
    const text = line.replace(/^[-*•]\s*/, '').replace(/\*\*/g, '');
    if (/^[-*•]\s/.test(line)) {
      body.insertListItem(i++, text).setGlyphType(DocumentApp.GlyphType.BULLET);
    } else {
      body.insertParagraph(i++, text);
    }
  });
  if (entry.briefing_url) {
    body.insertParagraph(i++, 'Public briefing').setLinkUrl(entry.briefing_url);
  }
  body.insertHorizontalRule(i++);
  doc.saveAndClose();
  return doc;
}

function openNotesDoc() {
  const props = PropertiesService.getScriptProperties();
  const id    = props.getProperty('NOTES_DOC_ID');
  if (id) {
    try {
      return DocumentApp.openById(id);
    } catch (e) {
      Logger.log('Notes doc missing, creating a new one: ' + e.message);
    }
  }
  const doc = DocumentApp.create(NOTES_DOC_TITLE);
  moveFileToFolder(doc.getId(), TARGET_FOLDER_ID);
  props.setProperty('NOTES_DOC_ID', doc.getId());
  return doc;
}

function sha256Hex(text) {
  return Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, text, Utilities.Charset.UTF_8)
    .map(b => ((b + 256) % 256).toString(16).padStart(2, '0'))
    .join('');
}

function jsonOut(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
