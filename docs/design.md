# Design

## Surfaces

Discord is the main interface. A listener opens a Spotify link from Discord,
sees one short confirmation page on a phone or desktop, and goes back to the
chat. The callback page follows the OS color preference, so it looks right
both in daylight and late in the evening.

## Color

Restrained neutrals with a muted green accent for actions. Discord cards use
one consistent green border. Warnings and failures always carry explicit text.
The callback page uses OKLCH neutrals and a green link with a visible focus
outline.

## Typography and layout

Discord cards use Discord's own typography: one title, an artist line, an
optional album thumbnail and a short footer. Queues read from top to bottom
with the next song first. The callback page uses the system sans stack in a
single column no wider than 38rem, with comfortable line height and a small
wordmark above the result.

## Components

Play is the primary action. Refresh, Queue, Skip and Stop are secondary.
Account linking uses a private link button, and device selection uses a native
select menu. Personal control panels belong to the listener who opened them
and expire with their controls disabled. Public now-playing cards let each
person open their own controls.

## Motion

There is no decorative animation. The bot relies on Discord's standard
interaction feedback, and the callback page has short focus and hover
transitions.
