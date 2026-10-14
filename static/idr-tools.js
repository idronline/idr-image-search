/* Shared behaviour for the IDR tools (Translate, Audio, Email Collector, Image Search).
   The same file is copied into every tool repo - change it in all four.
   - .drop: drag-and-drop file box that shows the chosen file's name and size
   - form[data-working="<id>"]: submit button stays disabled until the form is complete;
     on submit the form is replaced by the #<id> "working" card with a running timer */
(function () {
  function size(bytes) {
    return bytes < 1048576 ? Math.max(1, Math.round(bytes / 1024)) + ' KB' : (bytes / 1048576).toFixed(1) + ' MB';
  }

  // A file dropped outside a drop box would make the browser open it and leave the page.
  ['dragover', 'drop'].forEach(function (e) {
    window.addEventListener(e, function (ev) { ev.preventDefault(); });
  });

  document.querySelectorAll('.drop').forEach(function (drop) {
    var input = drop.querySelector('input[type=file]');
    function show() {
      var f = input.files[0];
      drop.classList.toggle('has-file', !!f);
      if (f) {
        drop.querySelector('.file-name').textContent = f.name;
        drop.querySelector('.file-size').textContent = size(f.size);
        drop.querySelector('.file-icon').textContent = (f.name.split('.').pop() || 'file').slice(0, 4);
      }
      input.dispatchEvent(new Event('input', { bubbles: true }));
    }
    input.addEventListener('change', show);
    ['dragenter', 'dragover'].forEach(function (e) {
      drop.addEventListener(e, function () { drop.classList.add('over'); });
    });
    ['dragleave', 'drop'].forEach(function (e) {
      drop.addEventListener(e, function () { drop.classList.remove('over'); });
    });
    drop.addEventListener('drop', function (ev) {
      if (ev.dataTransfer.files.length) { input.files = ev.dataTransfer.files; show(); }
    });
    show();
  });

  document.querySelectorAll('form[data-working]').forEach(function (form) {
    var button = form.querySelector('button[type=submit]');
    var hint = form.querySelector('.submit-hint');
    function check() {
      var ok = form.checkValidity();
      button.disabled = !ok;
      if (hint) hint.classList.toggle('hidden', ok);
    }
    form.addEventListener('input', check);
    form.addEventListener('change', check);
    check();

    var working = document.getElementById(form.dataset.working), ticking = null;
    form.addEventListener('submit', function () {
      document.querySelectorAll('.result').forEach(function (r) { r.classList.add('hidden'); });
      form.closest('.card').classList.add('hidden');
      working.classList.remove('hidden');
      var start = Date.now(), timer = working.querySelector('.timer');
      ticking = setInterval(function () {
        var s = Math.floor((Date.now() - start) / 1000);
        timer.textContent = Math.floor(s / 60) + ':' + ('0' + (s % 60)).slice(-2);
      }, 1000);
    });
    // Coming back with the browser's Back button: show the form again, not a frozen "working" card.
    window.addEventListener('pageshow', function (ev) {
      if (!ev.persisted) return;
      clearInterval(ticking);
      working.classList.add('hidden');
      form.closest('.card').classList.remove('hidden');
    });
  });
})();
