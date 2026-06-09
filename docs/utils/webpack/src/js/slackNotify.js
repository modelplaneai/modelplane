/*
* Create a PopperJS popover for the Join Slack notifications.
*/

import { createPopper } from '@popperjs/core';

const slackIcon = document.querySelector('#slack');
const tooltip = document.querySelector('#tooltip');

createPopper(slackIcon, tooltip, {
  modifiers: [
    {
      name: 'offset',
      options: {
        offset: [0, 12],
      },
    },
  ],
});

