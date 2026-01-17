'use strict';
// PyPowerwall Server - DAKboard Theme
// Transparent background optimized for DAKboard displays
window.pypowerwallTheme = 'dakboard';
console.log('DAKboard theme loaded');

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
        "background-color": "black",
    });

    $('.power-flow-grid.active').css({
        "background-color": "#000",
    });
}