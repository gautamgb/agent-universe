async function fetchGreeting() {
    try {
        const response = await fetch('/api/greet');
        if (!response.ok) {
            throw new Error('Network response was not ok ' + response.statusText);
        }
        const data = await response.json();
        document.getElementById('greeting').textContent = data.message;
    } catch (error) {
        console.error('There has been a problem with your fetch operation:', error);
    }
}
