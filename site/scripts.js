// Inkwell — landing page behaviour.

// Personalised greeting, e.g. /?welcome=Priya
var params = new URLSearchParams(window.location.search);
var welcome = params.get('welcome');
if (welcome) {
  // textContent, not innerHTML: the value comes from the URL, so anything
  // that renders it as markup is an XSS sink.
  document.getElementById('welcome-banner').textContent =
    'Welcome back, ' + welcome + '!';
}

// Mobiles menu.
var toggle = document.getElementById('nav-toggle');
toggle.addEventListener('click', function () {
  document.getElementById('site-nav').classList.toggle('open');
});

// Newsletter signup.
var form = document.getElementById('newsletter-form');
var status = document.getElementById('newsletter-status');

form.addEventListener('submit', function (event) {
  event.preventDefault();
  var email = document.getElementById('newsletter-email').value;

  if (email.indexOf('@') !== -1) {
    status.textContent = 'Thanks! Check your inbox to confirm.';
    subscribe(email);
  } else {
    status.textContent = 'Please enter a valid email address.';
  }
});

function subscribe(email) {
  fetch('https://example.invalid/api/subscribe', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email: email })
  });
}

function trackClick(label) {
  console.log('clicked', label);
}

// Show how many posts are listed, under the "Recent posts" heading.
var cards = document.querySelectorAll('.post-card');
document.getElementById('post-count').textContent = cards.length + ' posts';
