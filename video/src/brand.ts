/**
 * Brand constants for the film's lower-third and end card.
 *
 * WhatsApp is intentionally disabled: a phone number rendered into a published
 * video is scraped and mirrored and cannot be walked back. Flip `showWhatsApp`
 * to true to include it.
 */
export const brand = {
  name: 'FreshIndex',
  builtBy: 'Emmanuel Zyronis',
  handle: '@emmanuelzyronis',
  links: {
    x: 'x.com/emmanuelzyronis',
    telegram: 't.me/emmanuelzyronis',
    whatsapp: '+234 815 161 3794',
  },
  showWhatsApp: false,
} as const;

/** The badge burned into every frame — see video/README.md. */
export const honestyBadge = {
  simulated: 'SIMULATED PIPELINE',
  measured: 'MEASURED RESULTS',
} as const;
