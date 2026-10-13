# Product

## Who it is for

Discord users who listen to Hungary's Rádió 1 and want its songs on their own
Spotify devices. They come to the bot from Discord, usually with Spotify
already open somewhere.

## What it does

A listener connects a Spotify account, chooses a device, and follows the
station's song history. The station supplies the metadata and Spotify supplies
the playback. The bot is a personal Spotify remote. It is not a Discord voice
player, and it does not reproduce the live stream.

## Tone

Direct, familiar and quiet. The bot speaks English and looks like the rest of
Discord: compact song cards, standard buttons, plain wording and text labels on
every control.

## What to avoid

- Oversized embeds and status fields that repeat each other.
- Custom emoji that exist on only one server.
- Account details in public messages.
- A channel message for every song.
- Decorative terminal windows or promotional copy on the OAuth callback page.

## Principles

- Show the song and the useful action together.
- Keep account and device controls private.
- Explain what playback will do before it starts.
- Say so when radio data is unavailable or stale.
- Let a listener pause or take over playback without the bot fighting back.

## Accessibility

Use Discord's native controls with visible text labels, and never rely on color
alone to show status. The account-linking page uses semantic HTML, keyboard
focus styles, readable contrast and responsive spacing, and none of it depends
on animation.
