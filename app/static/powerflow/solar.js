'use strict';
// PyPowerwall Server - Solar Only Theme
// Hides battery display for solar-only installations
window.pypowerwallTheme = 'solar';
console.log('Solar theme loaded');

    // Hide Powerwall image 
    var imgElement = document.querySelector('[data-testid="b3372156-8a9e-4d17-9721-fcc5891d1074"]');
    if (imgElement) {
        imgElement.style.display = 'none';
    }
    // Hide the Powerwall text
    const divs = document.querySelectorAll('[data-testid="ec7d6a6d-b6d2-411c-a535-c052c00baf62"]');
    divs.forEach(div => {
        if (div.style.width === '120px' && div.style.top === '200.5px' && div.style.left === '0px' && div.style.right === '0px') {
            const paragraph = div.querySelector('p[data-testid="4c6aadb3-7661-4d7f-b1ff-d5a0571fac60"]');
            if (paragraph) {
                paragraph.style.display = 'none';
            }
        }
    });

    // Set alignment
    $('.core-layout__viewport').css({
        padding: 0,
        margin: 0,
    });

    $('.power-flow-header').css({
        "padding-top": 0,
    });

    $('.power-flow-grid').css({
        width: "100%",
        left: 0,
        right: 0,
        margin: 0,
        "padding-top": 0,
        "position": "fixed",
    });

    $('.app').css({
        "overflow-y": "hidden",
    });

    // Set colors
    $('body').css({
        "background-color": "transparent",
    });

    $('.power-flow-grid.active').css({
        "background-color": "transparent",
    });
}
