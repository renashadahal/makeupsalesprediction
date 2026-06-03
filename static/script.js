// Function to fetch sales data and render the Chart.js line chart
async function fetchSalesData() {
    try {
        const response = await fetch('/api/sales-data');
        const data = await response.json();
        
        // Assuming data is in the format { labels: [...], values: [...] }
        renderChart(data.labels, data.values);
    } catch (error) {
        console.error('Error fetching sales data:', error);
    }
}

// Function to render the Chart.js line chart
function renderChart(labels, values) {
    const ctx = document.getElementById('salesChart').getContext('2d');
    
    const salesChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: labels,
            datasets: [{
                label: 'Sales Data',
                data: values,
                borderColor: '#FF4B4B',
                backgroundColor: 'rgba(255, 75, 75, 0.2)',
                fill: true,
            }]
        },
        options: {
            responsive: true,
            scales: {
                x: {
                    title: {
                        display: true,
                        text: 'Date'
                    }
                },
                y: {
                    title: {
                        display: true,
                        text: 'Sales'
                    }
                }
            }
        }
    });
}

// Call the function to fetch data and render the chart
fetchSalesData();