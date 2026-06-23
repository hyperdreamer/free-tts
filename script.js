document.addEventListener('DOMContentLoaded', () => {
    const downloadBtn = document.getElementById('downloadBtn');
    const ssmlInput = document.getElementById('ssmlInput');

    // The default SSML text is already set in index.html, but you can also set it here:
    // ssmlInput.value = `<speak xmlns="http://www.w3.org/2001/10/synthesis" ... </speak>`;

    downloadBtn.addEventListener('click', async () => {
        const ssmlText = ssmlInput.value;

        if (!ssmlText.trim()) {
            alert('Please enter SSML text before downloading.');
            return;
        }

        // Provide user feedback
        downloadBtn.textContent = 'Generating...';
        downloadBtn.disabled = true;

        try {
            // IMPORTANT: Replace 'http://localhost:5000/generate-and-download-tts'
            // with the actual URL of your backend endpoint.
            const backendUrl = 'http://localhost:5000/generate-and-download-tts'; 

            const response = await fetch(backendUrl, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ ssml: ssmlText }),
            });

            if (response.ok) {
                // If the backend sends the MP3 file directly, create a blob and download it
                const blob = await response.blob();
                const url = window.URL.createObjectURL(blob);
                const a = document.createElement('a'); // Create a temporary anchor element
                a.style.display = 'none'; // Hide the anchor
                a.href = url;
                a.download = 'abc.mp3'; // The desired filename for the download
                document.body.appendChild(a);
                a.click(); // Programmatically click the anchor to trigger download
                window.URL.revokeObjectURL(url); // Clean up the object URL
                // Optionally provide success feedback
                // alert('MP3 downloaded successfully!'); 
            } else {
                // Handle errors from the backend
                const errorData = await response.json(); // Assuming backend sends JSON error
                alert(`Error: ${errorData.error || response.statusText}`);
            }
        } catch (error) {
            console.error('Download failed:', error);
            alert('Failed to connect to the server or download the file. Check server status and console for details.');
        } finally {
            // Reset button state regardless of success or failure
            downloadBtn.textContent = 'Download'; // Reset to "Download"
            downloadBtn.disabled = false;
        }
    });
});
