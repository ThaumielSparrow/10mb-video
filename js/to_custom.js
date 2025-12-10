(choice) => {
    if (choice === "Custom") {
        // Hide Strict, Show Editable (and reset its value)
        return [
            {visible: false, __type__: "update"},
            {visible: true, value: null, __type__: "update"}
        ];
    }
    // Keep Strict visible, Editable hidden
    return [
        {visible: true, __type__: "update"},
        {visible: false, __type__: "update"}
    ];
}