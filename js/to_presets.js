(choice) => {
    var presets = ["8 MB", "10 MB", "25 MB", "50 MB"];
    
    if (presets.includes(choice)) {
        // Swap back to Strict mode with this value selected
        return [
            {value: choice, visible: true, __type__: "update"},
            {visible: false, __type__: "update"}
        ];
    }
    
    // Keep Custom mode active
    return [
        {visible: false, __type__: "update"}, // Strict stays hidden
        {visible: true, __type__: "update"}   // Editable stays visible
    ];
}